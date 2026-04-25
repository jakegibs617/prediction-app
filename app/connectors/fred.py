"""
FRED (Federal Reserve Economic Data) connector — macro indicator time series.

Endpoints used:
  /fred/series/observations — fetch recent observations for a series

Free tier: generous rate limits (~120 req/min). No per-day hard cap documented.
We insert a 1-second sleep between series fetches to be polite.

Anti-lookahead: FRED observations include a `realtime_start` field which is the
date the value became the current vintage on FRED (i.e. its release date).
We store this as `released_at` so the feature engine uses the correct
availability timestamp and never looks ahead at unreleased revisions.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)

BASE_URL = "https://api.stlouisfed.org/fred"


@dataclass
class FredSeries:
    series_id: str   # FRED series identifier e.g. "CPIAUCSL"
    name: str        # Human-readable name
    subtype: str     # Logical grouping e.g. "cpi", "interest_rate"


TRACKED_SERIES: list[FredSeries] = [
    FredSeries("CPIAUCSL", "CPI: All Urban Consumers (Seasonally Adjusted)", "cpi"),
    FredSeries("FEDFUNDS", "Federal Funds Effective Rate",                    "interest_rate"),
    FredSeries("UNRATE",   "Unemployment Rate",                               "employment"),
    FredSeries("GDP",      "Gross Domestic Product",                          "gdp"),
    FredSeries("DGS10",    "10-Year Treasury Constant Maturity Rate",         "interest_rate"),
    FredSeries("T10YIE",   "10-Year Breakeven Inflation Rate",                "inflation"),
    FredSeries("DCOILWTICO", "WTI Crude Oil Price",                           "commodity_price"),
]


class FredConnector(BaseConnector):
    source_name = "fred"
    category = "macro"
    base_url = BASE_URL
    auth_type = "api_key"
    trust_level = "unverified"
    rate_limit_per_minute = 120

    def _api_key(self) -> str:
        key = settings.fred_api_key
        if not key or key.startswith("REPLACE"):
            raise RuntimeError("FRED_API_KEY is not set in .env")
        return key

    async def _fetch_observations(self, series_id: str, limit: int = 20) -> list[dict]:
        resp = await fetch_with_retry(
            f"{BASE_URL}/series/observations",
            params={
                "series_id": series_id,
                "api_key": self._api_key(),
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            },
            max_attempts=3,
        )
        data = resp.json()
        if "error_message" in data:
            raise RuntimeError(f"FRED error for {series_id}: {data['error_message']}")
        observations = data.get("observations", [])
        # Filter out missing values (FRED uses "." for not-yet-released)
        return [o for o in observations if o.get("value") not in (".", "", None)]

    async def run(self) -> None:
        job_id = await acquire_job_lock("macro_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            total_raw = 0

            for i, series in enumerate(TRACKED_SERIES):
                if i > 0:
                    await asyncio.sleep(1)

                log.info("fred_fetch_start", series_id=series.series_id)
                try:
                    observations = await self._fetch_observations(series.series_id)
                except Exception as exc:
                    log.error("fred_fetch_failed", series_id=series.series_id, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    for obs in observations:
                        obs_date = obs["date"]
                        external_id = f"{series.series_id}::{obs_date}"
                        raw_payload = {
                            "series_id": series.series_id,
                            "series_name": series.name,
                            "subtype": series.subtype,
                            "observation_date": obs_date,
                            "value": obs["value"],
                            "vintage_date": obs.get("realtime_start"),
                        }
                        write_plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if not write_plan.should_write:
                            continue

                        # source_recorded_at = observation period end
                        source_recorded_at = datetime.strptime(obs_date, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                        # released_at = realtime_start = when FRED first published this vintage
                        realtime_start = obs.get("realtime_start")
                        released_at = (
                            datetime.strptime(realtime_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            if realtime_start and realtime_start != "9999-12-31"
                            else None
                        )

                        await self.write_raw_record(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                            source_recorded_at=source_recorded_at,
                            released_at=released_at,
                            record_version=write_plan.record_version,
                            prior_record_id=write_plan.prior_record_id,
                            checksum=write_plan.checksum,
                        )
                        total_raw += 1

                log.info("fred_series_done", series_id=series.series_id, new_records=total_raw)

            await release_job_lock(job_id, "succeeded")
            log.info("fred_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("fred_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
