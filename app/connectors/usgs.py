"""
USGS Earthquake connector — significant seismic events as market signals.

API used: USGS FDSN Event Web Service (no API key required)
  https://earthquake.usgs.gov/fdsnws/event/1/query

No API key required. A descriptive User-Agent is polite but not enforced.

Only M5.0+ earthquakes are fetched to filter out minor tremors. Significant
quakes are market-relevant signals:
  - Gulf of Mexico / Texas coast → energy infrastructure disruption
  - Japan / Taiwan → semiconductor / electronics supply chain
  - California → tech sector, insurance exposure
  - Middle East → oil pipeline / port disruption

Events are stored with category='events'. The normalization pipeline runs
LLM extraction for sentiment/entity metadata.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# Minimum magnitude to ingest — M5.0+ is "strong", felt widely, infrastructure risk
MIN_MAGNITUDE = 5.0


def _build_description(props: dict, coords: list) -> str:
    """Synthesize human-readable text for the normalization LLM."""
    place = props.get("place", "Unknown location")
    mag = props.get("mag", "")
    mag_type = props.get("magType", "")
    depth = coords[2] if len(coords) >= 3 else ""
    tsunami = props.get("tsunami", 0)
    alert = props.get("alert", "")
    sig = props.get("sig", "")

    parts = [f"M{mag} {mag_type} earthquake near {place}."]
    if depth:
        parts.append(f"Depth: {depth} km.")
    if tsunami:
        parts.append("Tsunami warning issued.")
    if alert:
        parts.append(f"PAGER alert level: {alert}.")
    if sig:
        parts.append(f"Significance score: {sig}.")
    return " ".join(parts)


class UsgsConnector(BaseConnector):
    source_name = "usgs_earthquakes"
    category = "events"
    base_url = USGS_URL
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 30

    async def _fetch_earthquakes(self, start_time: str, end_time: str) -> list[dict]:
        resp = await fetch_with_retry(
            USGS_URL,
            headers={"User-Agent": settings.app_user_agent},
            params={
                "format": "geojson",
                "starttime": start_time,
                "endtime": end_time,
                "minmagnitude": str(MIN_MAGNITUDE),
                "orderby": "time",
            },
            max_attempts=3,
        )
        data = resp.json()
        return data.get("features", [])

    async def run(self) -> None:
        job_id = await acquire_job_lock("events_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            now = get_utc_now()
            # 48h window to avoid missing events across day boundaries
            start_time = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
            end_time = now.strftime("%Y-%m-%dT%H:%M:%S")

            log.info("usgs_fetch_start", min_magnitude=MIN_MAGNITUDE, start=start_time)
            try:
                features = await self._fetch_earthquakes(start_time, end_time)
            except Exception as exc:
                log.error("usgs_fetch_failed", error=str(exc))
                await release_job_lock(job_id, "failed")
                return

            total_raw = 0

            async with pool.acquire() as conn:
                for feature in features:
                    event_id = feature.get("id", "")
                    if not event_id:
                        continue

                    props = feature.get("properties") or {}
                    geom = feature.get("geometry") or {}
                    coords = geom.get("coordinates") or []

                    external_id = f"usgs::{event_id}"

                    # USGS time is Unix milliseconds
                    event_time_ms = props.get("time")
                    event_dt = (
                        datetime.fromtimestamp(event_time_ms / 1000, tz=timezone.utc)
                        if event_time_ms else None
                    )
                    updated_ms = props.get("updated")
                    updated_dt = (
                        datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc)
                        if updated_ms else event_dt
                    )

                    description = _build_description(props, coords)
                    raw_payload = {
                        "usgs_id": event_id,
                        "magnitude": props.get("mag"),
                        "mag_type": props.get("magType", ""),
                        "place": props.get("place", ""),
                        "depth_km": coords[2] if len(coords) >= 3 else None,
                        "longitude": coords[0] if len(coords) >= 1 else None,
                        "latitude": coords[1] if len(coords) >= 2 else None,
                        "tsunami": props.get("tsunami", 0),
                        "alert": props.get("alert", ""),
                        "sig": props.get("sig", 0),
                        "event_type": props.get("type", "earthquake"),
                        "url": props.get("url", ""),
                        "title": props.get("title", ""),
                        "description": description,
                    }
                    write_plan = await self.plan_raw_record_write(
                        conn,
                        source_id=source_id,
                        external_id=external_id,
                        raw_payload=raw_payload,
                    )
                    if not write_plan.should_write:
                        continue

                    await self.write_raw_record(
                        conn,
                        source_id=source_id,
                        external_id=external_id,
                        raw_payload=raw_payload,
                        source_recorded_at=event_dt,
                        released_at=updated_dt,
                        published_at=event_dt,
                        record_version=write_plan.record_version,
                        prior_record_id=write_plan.prior_record_id,
                        checksum=write_plan.checksum,
                    )
                    total_raw += 1

            await release_job_lock(job_id, "succeeded")
            log.info("usgs_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("usgs_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
