"""
World Bank connector — annual macroeconomic indicators for major economies.

API used: World Bank Indicators API v2 (no API key required)
  https://api.worldbank.org/v2/country/{country}/indicator/{indicator}?format=json

No API key required. Polite User-Agent is recommended.

World Bank publishes annual data for 200+ countries with 1-2 year lag.
It complements FRED (US-only, higher frequency) with international context:
  - China GDP growth signals global demand trends
  - Trade openness % shows exposure to tariff shocks
  - Debt-to-GDP ratios flag sovereign risk

Records are stored with category='macro' — the normalization pipeline marks
them valid immediately without LLM extraction.

Anti-lookahead: we store the observation year's Jan 1 as released_at. World
Bank data is already substantially lagged, so this is conservative.
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

WB_BASE = "https://api.worldbank.org/v2"

# (country_code, human_readable_name)
TRACKED_COUNTRIES: list[tuple[str, str]] = [
    ("US", "United States"),
    ("CN", "China"),
    ("JP", "Japan"),
    ("GB", "United Kingdom"),
    ("DE", "Germany"),
]

# (indicator_code, human_readable_name, subtype)
@dataclass
class WbIndicator:
    code: str
    name: str
    subtype: str


TRACKED_INDICATORS: list[WbIndicator] = [
    WbIndicator("NY.GDP.MKTP.KD.ZG", "GDP growth (annual %)",                 "gdp_growth"),
    WbIndicator("FP.CPI.TOTL.ZG",    "Inflation, consumer prices (annual %)", "inflation"),
    WbIndicator("SL.UEM.TOTL.ZS",    "Unemployment, total (% labor force)",   "unemployment"),
    WbIndicator("NE.TRD.GNFS.ZS",    "Trade (% of GDP)",                      "trade"),
    WbIndicator("GC.DOD.TOTL.GD.ZS", "Central government debt (% of GDP)",    "govt_debt"),
]

# Fetch this many recent observations per country/indicator pair
_RECENT_OBS = 5


def _obs_released_at(year_str: str) -> datetime | None:
    """Use Jan 1 of the observation year as a conservative released_at timestamp."""
    try:
        return datetime(int(year_str), 1, 1, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class WorldBankConnector(BaseConnector):
    source_name = "world_bank"
    category = "macro"
    base_url = WB_BASE
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 30

    async def _fetch_observations(self, country: str, indicator: str) -> list[dict]:
        url = f"{WB_BASE}/country/{country}/indicator/{indicator}"
        resp = await fetch_with_retry(
            url,
            headers={"User-Agent": settings.app_user_agent},
            params={
                "format": "json",
                "mrv": str(_RECENT_OBS),  # most recent N values
                "per_page": str(_RECENT_OBS),
            },
            max_attempts=3,
        )
        data = resp.json()
        # World Bank wraps response: [metadata_dict, [observations]]
        if isinstance(data, list) and len(data) >= 2:
            return data[1] or []
        return []

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
            request_count = 0

            for country_code, country_name in TRACKED_COUNTRIES:
                for indicator in TRACKED_INDICATORS:
                    if request_count > 0:
                        await asyncio.sleep(1)
                    request_count += 1

                    log.info(
                        "wb_fetch_start",
                        country=country_code,
                        indicator=indicator.code,
                    )
                    try:
                        observations = await self._fetch_observations(
                            country_code, indicator.code
                        )
                    except Exception as exc:
                        log.error(
                            "wb_fetch_failed",
                            country=country_code,
                            indicator=indicator.code,
                            error=str(exc),
                        )
                        continue

                    async with pool.acquire() as conn:
                        for obs in observations:
                            value = obs.get("value")
                            date = obs.get("date", "")  # e.g. "2023"

                            # Skip observations with no value (unreleased/missing)
                            if value is None:
                                continue

                            external_id = f"wb::{country_code}::{indicator.code}::{date}"
                            raw_payload = {
                                "country_code": country_code,
                                "country_name": country_name,
                                "indicator_code": indicator.code,
                                "indicator_name": indicator.name,
                                "subtype": indicator.subtype,
                                "date": date,
                                "value": value,
                                "unit": obs.get("unit", ""),
                                "obs_status": obs.get("obs_status", ""),
                            }
                            write_plan = await self.plan_raw_record_write(
                                conn,
                                source_id=source_id,
                                external_id=external_id,
                                raw_payload=raw_payload,
                            )
                            if not write_plan.should_write:
                                continue

                            released_at = _obs_released_at(date)

                            await self.write_raw_record(
                                conn,
                                source_id=source_id,
                                external_id=external_id,
                                raw_payload=raw_payload,
                                source_recorded_at=released_at,
                                released_at=released_at,
                                published_at=released_at,
                                record_version=write_plan.record_version,
                                prior_record_id=write_plan.prior_record_id,
                                checksum=write_plan.checksum,
                            )
                            total_raw += 1

            await release_job_lock(job_id, "succeeded")
            log.info("wb_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("wb_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
