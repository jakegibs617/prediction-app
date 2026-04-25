"""
IMF World Economic Outlook connector — annual macro indicators for major economies.

API used: IMF Data Services SDMX-JSON (no API key required)
  https://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData/WEO/{series}

No API key required. The SDMX_JSON endpoint is publicly accessible.

Multiple countries and indicators can be combined in one request using '+':
  USA+CHN+JPN.NGDP_RPCH+PCPIPCH+LUR?startPeriod=2015&endPeriod=2024

WEO data is annual with a 1-2 year lag. It complements FRED (US-only, high
frequency) and World Bank with IMF's own growth/debt/inflation forecasts,
which are widely cited as market-moving signals when revised.

Records are stored with category='macro' — validated immediately, no LLM.

Anti-lookahead: Jan 1 of the observation year is used as released_at.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)

IMF_BASE = "https://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData/WEO"

TRACKED_COUNTRIES: list[str] = ["USA", "CHN", "JPN", "GBR", "DEU"]

# (indicator_code, human_readable_name)
TRACKED_INDICATORS: list[tuple[str, str]] = [
    ("NGDP_RPCH",   "Real GDP growth rate (%)"),
    ("PCPIPCH",     "Inflation, avg consumer prices (%)"),
    ("LUR",         "Unemployment rate (%)"),
    ("BCA_NGDPD",   "Current account balance (% of GDP)"),
    ("GGXWDG_NGDP", "General government gross debt (% of GDP)"),
]

_START_PERIOD = "2015"


def _obs_released_at(year_str: str) -> datetime | None:
    try:
        return datetime(int(year_str), 1, 1, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _indicator_name(code: str) -> str:
    return next((name for c, name in TRACKED_INDICATORS if c == code), code)


class ImfConnector(BaseConnector):
    source_name = "imf_weo"
    category = "macro"
    base_url = IMF_BASE
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 20

    async def _fetch_series(
        self, countries: list[str], indicators: list[str], end_period: str
    ) -> list[dict]:
        """Fetch all country+indicator combos in one request. Returns list of series dicts."""
        country_str = "+".join(countries)
        indicator_str = "+".join(indicators)
        url = f"{IMF_BASE}/{country_str}.{indicator_str}"

        resp = await fetch_with_retry(
            url,
            headers={
                "User-Agent": settings.app_user_agent,
                "Accept": "application/json",
            },
            params={"startPeriod": _START_PERIOD, "endPeriod": end_period},
            max_attempts=3,
        )
        data = resp.json()
        series = (
            data.get("CompactData", {})
            .get("DataSet", {})
            .get("Series", [])
        )
        # Single-series responses return a dict; normalize to list
        if isinstance(series, dict):
            series = [series]
        return series or []

    async def run(self) -> None:
        job_id = await acquire_job_lock("macro_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            end_period = str(datetime.now(timezone.utc).year)
            indicator_codes = [code for code, _ in TRACKED_INDICATORS]

            log.info(
                "imf_fetch_start",
                countries=TRACKED_COUNTRIES,
                indicators=indicator_codes,
            )
            try:
                all_series = await self._fetch_series(
                    TRACKED_COUNTRIES, indicator_codes, end_period
                )
            except Exception as exc:
                log.error("imf_fetch_failed", error=str(exc))
                await release_job_lock(job_id, "failed")
                return

            total_raw = 0

            async with pool.acquire() as conn:
                for series in all_series:
                    country = series.get("@REF_AREA", "")
                    indicator = series.get("@INDICATOR", "")
                    indicator_name = _indicator_name(indicator)
                    scale = series.get("@SCALE", "")
                    unit = series.get("@UNIT_MULT", "")

                    obs_list = series.get("Obs", [])
                    # Single observation comes back as dict, not list
                    if isinstance(obs_list, dict):
                        obs_list = [obs_list]

                    for obs in obs_list:
                        value = obs.get("@OBS_VALUE")
                        period = obs.get("@TIME_PERIOD", "")

                        if value is None or not period:
                            continue

                        external_id = f"imf::{country}::{indicator}::{period}"
                        raw_payload = {
                            "country_code": country,
                            "indicator_code": indicator,
                            "indicator_name": indicator_name,
                            "period": period,
                            "value": value,
                            "scale": scale,
                            "unit_mult": unit,
                            "obs_status": obs.get("@OBS_STATUS", ""),
                        }
                        write_plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if not write_plan.should_write:
                            continue

                        released_at = _obs_released_at(period)

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
            log.info("imf_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("imf_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
