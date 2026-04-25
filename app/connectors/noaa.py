"""
NOAA / National Weather Service connector — severe weather alerts as event signals.

API used: NWS Public API (no API key required)
  https://api.weather.gov/alerts/active

No API key required. NWS asks for a descriptive User-Agent header with a
contact email (see NOAA_USER_AGENT in .env).

Rate limit: NWS asks for polite use (no hard limit stated). We run at most
once per hour and fetch a single paginated endpoint, so we stay well under.

Severe weather alerts (Extreme/Severe severity, status=actual) are
market-relevant signals:
  - Gulf Coast hurricanes → energy production / shipping disruption
  - Midwest floods / droughts → agricultural commodity price impact
  - California wildfires → utility stocks, insurance exposure
  - Northeast blizzards → demand spikes for heating oil / nat gas

Alerts are stored with category='events'. The normalization pipeline runs
LLM extraction to produce structured sentiment/entity metadata.

Only severity=Extreme|Severe with status=actual is fetched to avoid flooding
the pipeline with routine advisories.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

Severity = Literal["Extreme", "Severe"]
_FETCH_SEVERITIES: list[Severity] = ["Extreme", "Severe"]


def _user_agent() -> str:
    ua = settings.noaa_user_agent
    if not ua or ua.startswith("REPLACE"):
        raise RuntimeError(
            "NOAA_USER_AGENT is not set in .env. "
            "Format: 'AppName/Version contact@email.com'"
        )
    return ua


def _parse_nws_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _build_description(props: dict) -> str:
    """Synthesize human-readable text from NWS alert properties for the normalization LLM."""
    event = props.get("event", "Severe Weather Alert")
    area = props.get("areaDesc", "")
    headline = props.get("headline") or ""
    sender = props.get("senderName", "")
    severity = props.get("severity", "")
    certainty = props.get("certainty", "")
    effective = props.get("effective", "")
    expires = props.get("expires", "")

    parts = [f"NWS {event} issued by {sender}."]
    if area:
        parts.append(f"Affected area: {area}.")
    if severity:
        parts.append(f"Severity: {severity}.")
    if certainty:
        parts.append(f"Certainty: {certainty}.")
    if effective:
        parts.append(f"Effective: {effective}.")
    if expires:
        parts.append(f"Expires: {expires}.")
    if headline:
        parts.append(headline)
    return " ".join(parts)


class NoaaConnector(BaseConnector):
    source_name = "noaa_nws"
    category = "events"
    base_url = NWS_ALERTS_URL
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 10

    async def _fetch_alerts(self, severity: str) -> list[dict]:
        resp = await fetch_with_retry(
            NWS_ALERTS_URL,
            headers={
                "User-Agent": _user_agent(),
                "Accept": "application/geo+json",
            },
            params={
                "status": "actual",
                "severity": severity,
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

            total_raw = 0

            for severity in _FETCH_SEVERITIES:
                log.info("noaa_fetch_start", severity=severity)
                try:
                    features = await self._fetch_alerts(severity)
                except Exception as exc:
                    log.error("noaa_fetch_failed", severity=severity, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    for feature in features:
                        alert_id = feature.get("id", "")
                        if not alert_id:
                            continue

                        props = feature.get("properties") or {}

                        # Skip non-actual statuses that may slip through
                        if props.get("status") != "actual":
                            continue

                        external_id = alert_id

                        effective_dt = _parse_nws_datetime(props.get("effective"))
                        expires_dt = _parse_nws_datetime(props.get("expires"))
                        sent_dt = _parse_nws_datetime(props.get("sent"))

                        description = _build_description(props)
                        headline = props.get("headline") or ""
                        full_description = props.get("description") or ""
                        raw_payload = {
                            "alert_id": alert_id,
                            "event": props.get("event", ""),
                            "severity": props.get("severity", ""),
                            "urgency": props.get("urgency", ""),
                            "certainty": props.get("certainty", ""),
                            "status": props.get("status", ""),
                            "area_desc": props.get("areaDesc", ""),
                            "sender_name": props.get("senderName", ""),
                            "effective": props.get("effective", ""),
                            "expires": props.get("expires", ""),
                            "headline": headline,
                            "description": full_description,
                            "summary": description,
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
                            source_recorded_at=effective_dt,
                            released_at=sent_dt or effective_dt,
                            published_at=sent_dt or effective_dt,
                            record_version=write_plan.record_version,
                            prior_record_id=write_plan.prior_record_id,
                            checksum=write_plan.checksum,
                        )
                        total_raw += 1

                log.info("noaa_severity_done", severity=severity, new_records=total_raw)

            await release_job_lock(job_id, "succeeded")
            log.info("noaa_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("noaa_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
