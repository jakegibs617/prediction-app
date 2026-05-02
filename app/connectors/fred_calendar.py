"""
FRED economic calendar connector — scheduled release dates.

Uses FRED's /fred/release/dates endpoint to fetch upcoming announcement dates
for FOMC, CPI, PPI, and Employment Situation (NFP). Stores each date as a
raw_source_record and a normalized_event (event_type='economic_release').

FRED release IDs used:
  10  CPI: Consumer Price Index
  21  FOMC press release
  31  PPI: Producer Price Index
  50  Employment Situation (NFP / Nonfarm Payrolls)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)

BASE_URL = "https://api.stlouisfed.org/fred"

_LOOKAHEAD_DAYS = 180


@dataclass
class FredRelease:
    release_id: int
    name: str
    subtype: str        # "fomc" | "cpi" | "ppi" | "nfp"
    severity_score: float


TRACKED_RELEASES: list[FredRelease] = [
    FredRelease(21, "FOMC Meeting Decision",       "fomc", 0.9),
    FredRelease(10, "CPI: Consumer Price Index",   "cpi",  0.8),
    FredRelease(31, "PPI: Producer Price Index",   "ppi",  0.6),
    FredRelease(50, "Employment Situation (NFP)",  "nfp",  0.7),
]


class FredCalendarConnector(BaseConnector):
    source_name = "fred_calendar"
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

    async def _fetch_release_dates(self, release: FredRelease) -> list[str]:
        """Return ISO date strings for upcoming release dates within the lookahead window."""
        today = datetime.now(tz=timezone.utc).date()
        end_date = today + timedelta(days=_LOOKAHEAD_DAYS)
        resp = await fetch_with_retry(
            f"{BASE_URL}/release/dates",
            params={
                "release_id": str(release.release_id),
                "api_key": self._api_key(),
                "file_type": "json",
                "sort_order": "asc",
                "realtime_start": today.isoformat(),
                "realtime_end": end_date.isoformat(),
                "include_release_dates_with_no_data": "true",
            },
            max_attempts=3,
        )
        data = resp.json()
        if "error_message" in data:
            raise RuntimeError(
                f"FRED calendar error for release {release.release_id}: {data['error_message']}"
            )
        return [d["date"] for d in data.get("release_dates", [])]

    async def run(self) -> None:
        job_id = await acquire_job_lock("calendar_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            total_written = 0

            for i, release in enumerate(TRACKED_RELEASES):
                if i > 0:
                    await asyncio.sleep(1)

                log.info("fred_calendar_fetch_start", release_id=release.release_id, name=release.name)
                try:
                    date_strings = await self._fetch_release_dates(release)
                except Exception as exc:
                    log.error(
                        "fred_calendar_fetch_failed",
                        release_id=release.release_id,
                        error=str(exc),
                    )
                    continue

                async with pool.acquire() as conn:
                    for date_str in date_strings:
                        external_id = f"fred_calendar::{release.release_id}::{date_str}"
                        raw_payload = {
                            "release_id": release.release_id,
                            "release_name": release.name,
                            "subtype": release.subtype,
                            "release_date": date_str,
                        }
                        write_plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if not write_plan.should_write:
                            continue

                        release_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                        record_id = await self.write_raw_record(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                            source_recorded_at=release_dt,
                            released_at=release_dt,
                            record_version=write_plan.record_version,
                            prior_record_id=write_plan.prior_record_id,
                            checksum=write_plan.checksum,
                        )

                        await conn.execute(
                            """
                            INSERT INTO ingestion.normalized_events
                                (source_record_id, event_type, event_subtype, title,
                                 summary, severity_score, event_occurred_at)
                            VALUES ($1, 'economic_release', $2, $3, $4, $5, $6)
                            """,
                            record_id,
                            release.subtype,
                            release.name,
                            f"Scheduled {release.name} on {date_str}",
                            release.severity_score,
                            release_dt,
                        )
                        total_written += 1

            await release_job_lock(job_id, "succeeded")
            log.info("fred_calendar_run_complete", total_written=total_written)

        except Exception as exc:
            log.error("fred_calendar_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
