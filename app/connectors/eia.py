"""
EIA (US Energy Information Administration) v2 API connector.

Endpoints used (all under https://api.eia.gov/v2/...):
  petroleum/pri/spt/data/   - daily spot prices (we fetch WTI Cushing OK)
  petroleum/stoc/wstk/data/ - weekly petroleum stocks (we fetch crude excluding SPR)
  petroleum/sum/snd/data/   - weekly motor gasoline supply
  natural-gas/stor/wkly/data/ - weekly natural gas underground storage

Free tier: requires a key (already provisioned as EIA_API_KEY in .env).
No documented hard rate limit; we sleep 1s between series fetches.

The EIA v2 schema returns rows with `period`, `value`, `series`, `units`,
`series-description`, etc. We store them in ingestion.raw_source_records
under category='macro' so the normalization pipeline validates structurally
and the feature engine can consume them.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import (
    acquire_job_lock,
    increment_attempt,
    release_job_lock,
    send_to_dead_letter,
)

log = structlog.get_logger(__name__)

BASE_URL = "https://api.eia.gov/v2"


@dataclass
class EiaRoute:
    series_id: str
    name: str
    subtype: str
    path: str        # the path under /v2/, e.g. "petroleum/pri/spt"
    frequency: str   # "daily" | "weekly"


TRACKED_ROUTES: list[EiaRoute] = [
    EiaRoute(
        series_id="RWTC",
        name="WTI Crude Oil Spot Price (Cushing OK FOB)",
        subtype="commodity_price_oil",
        path="petroleum/pri/spt",
        frequency="daily",
    ),
    EiaRoute(
        series_id="WCESTUS1",
        name="US Ending Stocks excluding SPR of Crude Oil",
        subtype="petroleum_inventory",
        path="petroleum/stoc/wstk",
        frequency="weekly",
    ),
    EiaRoute(
        series_id="WGTSTUS1",
        name="US Ending Stocks of Total Gasoline",
        subtype="petroleum_inventory",
        path="petroleum/stoc/wstk",
        frequency="weekly",
    ),
    EiaRoute(
        series_id="NW2_EPG0_SWO_R48_BCF",
        name="US Natural Gas Working Underground Storage Lower 48",
        subtype="natural_gas_storage",
        path="natural-gas/stor/wkly",
        frequency="weekly",
    ),
]


class EiaConnector(BaseConnector):
    source_name = "eia"
    category = "macro"
    base_url = BASE_URL
    auth_type = "api_key"
    trust_level = "verified"
    rate_limit_per_minute = 60

    def _api_key(self) -> str:
        key = settings.eia_api_key
        if not key or key.startswith("REPLACE") or key.startswith("#"):
            raise RuntimeError("EIA_API_KEY is not set in .env")
        return key

    async def _fetch_route(self, route: EiaRoute, length: int = 30) -> list[dict]:
        url = f"{BASE_URL}/{route.path}/data/"
        params = {
            "api_key": self._api_key(),
            "frequency": route.frequency,
            "data[0]": "value",
            "facets[series][]": route.series_id,
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "offset": 0,
            "length": length,
        }
        resp = await fetch_with_retry(url, params=params, max_attempts=3)
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"EIA error for {route.series_id}: {body['error']}")
        response_obj = body.get("response") or {}
        rows = response_obj.get("data") or []
        cleaned: list[dict] = []
        for row in rows:
            value = row.get("value")
            period = row.get("period")
            if value in (None, "", ".") or period in (None, ""):
                continue
            try:
                value_float = float(value)
            except (TypeError, ValueError):
                continue
            cleaned.append(
                {
                    "period": period,
                    "value": value_float,
                    "units": row.get("units"),
                    "series_description": row.get("series-description"),
                }
            )
        return cleaned

    @staticmethod
    def _parse_period(period: str, frequency: str) -> datetime:
        """EIA periods are 'YYYY-MM-DD' for daily/weekly and 'YYYY-MM' for monthly."""
        if frequency in ("daily", "weekly"):
            return datetime.strptime(period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if frequency == "monthly":
            return datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
        raise ValueError(f"Unsupported EIA frequency: {frequency!r}")

    async def run(self) -> None:
        job_id = await acquire_job_lock(
            "macro_ingest", source_name=self.source_name
        )
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            total_new = 0

            for i, route in enumerate(TRACKED_ROUTES):
                if i > 0:
                    await asyncio.sleep(1)

                log.info("eia_fetch_start", series_id=route.series_id)
                try:
                    rows = await self._fetch_route(route, length=60)
                except Exception as exc:
                    log.error(
                        "eia_fetch_failed",
                        series_id=route.series_id,
                        error=str(exc),
                    )
                    continue

                async with pool.acquire() as conn:
                    for row in rows:
                        external_id = f"{route.series_id}::{row['period']}"
                        raw_payload = {
                            "series_id": route.series_id,
                            "series_name": route.name,
                            "subtype": route.subtype,
                            "frequency": route.frequency,
                            "observation_date": row["period"],
                            "value": row["value"],
                            "units": row.get("units"),
                            "series_description": row.get("series_description"),
                        }
                        plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if not plan.should_write:
                            continue

                        period_dt = self._parse_period(row["period"], route.frequency)
                        await self.write_raw_record(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                            source_recorded_at=period_dt,
                            released_at=period_dt,
                            record_version=plan.record_version,
                            prior_record_id=plan.prior_record_id,
                            checksum=plan.checksum,
                        )
                        total_new += 1

                log.info(
                    "eia_series_done",
                    series_id=route.series_id,
                    fetched=len(rows),
                    total_new=total_new,
                )

            await release_job_lock(job_id, "succeeded")
            log.info("eia_run_complete", total_new=total_new)

        except Exception as exc:
            log.error("eia_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
