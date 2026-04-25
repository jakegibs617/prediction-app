"""
Alternative.me — Crypto Fear & Greed Index connector.

Endpoint: https://api.alternative.me/fng/?limit=N

No auth, no rate limits documented (community-funded). Returns a daily
sentiment score on a 0-100 scale where:
  0-24   = Extreme Fear
  25-49  = Fear
  50-54  = Neutral
  55-74  = Greed
  75-100 = Extreme Greed

We treat each daily reading as one record under category='macro' so the
normalization pipeline validates it structurally and feeds it into the
feature engine. The score is well-known to correlate with crypto reversal
points (extreme fear = oversold, extreme greed = overbought).
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog

from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import (
    acquire_job_lock,
    increment_attempt,
    release_job_lock,
    send_to_dead_letter,
)

log = structlog.get_logger(__name__)

BASE_URL = "https://api.alternative.me"
SERIES_ID = "FEAR_GREED"
SERIES_NAME = "Crypto Fear & Greed Index"
SUBTYPE = "sentiment_index"


class FearGreedConnector(BaseConnector):
    source_name = "alternative_me_fear_greed"
    category = "macro"
    base_url = BASE_URL
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 60

    async def _fetch(self, limit: int = 30) -> list[dict]:
        resp = await fetch_with_retry(
            f"{BASE_URL}/fng/",
            params={"limit": limit, "format": "json"},
            max_attempts=3,
        )
        body = resp.json()
        # Defensive: shape is {"name": "...", "data": [...], "metadata": {...}}
        meta = body.get("metadata") or {}
        if meta.get("error"):
            raise RuntimeError(f"alternative.me error: {meta['error']}")
        items = body.get("data") or []
        cleaned: list[dict] = []
        for item in items:
            try:
                value = int(item["value"])
                ts = int(item["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            cleaned.append(
                {
                    "value": value,
                    "value_classification": item.get("value_classification"),
                    "timestamp": ts,
                }
            )
        return cleaned

    async def run(self) -> None:
        # Daily index — pull last 30 readings each tick. Duplicate-by-checksum
        # in plan_raw_record_write means this is cheap to repeat.
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

            try:
                items = await self._fetch(limit=30)
            except Exception as exc:
                log.error("fear_greed_fetch_failed", error=str(exc))
                await release_job_lock(job_id, "failed", error_summary=str(exc))
                return

            new_records = 0
            async with pool.acquire() as conn:
                for item in items:
                    obs_dt = datetime.fromtimestamp(item["timestamp"], tz=timezone.utc)
                    obs_date = obs_dt.strftime("%Y-%m-%d")
                    external_id = f"{SERIES_ID}::{obs_date}"
                    raw_payload = {
                        "series_id": SERIES_ID,
                        "series_name": SERIES_NAME,
                        "subtype": SUBTYPE,
                        "observation_date": obs_date,
                        "value": item["value"],
                        "value_classification": item["value_classification"],
                        "scale_min": 0,
                        "scale_max": 100,
                        "applies_to_asset_type": "crypto",
                    }
                    plan = await self.plan_raw_record_write(
                        conn,
                        source_id=source_id,
                        external_id=external_id,
                        raw_payload=raw_payload,
                    )
                    if not plan.should_write:
                        continue

                    await self.write_raw_record(
                        conn,
                        source_id=source_id,
                        external_id=external_id,
                        raw_payload=raw_payload,
                        source_recorded_at=obs_dt,
                        released_at=obs_dt,
                        record_version=plan.record_version,
                        prior_record_id=plan.prior_record_id,
                        checksum=plan.checksum,
                    )
                    new_records += 1

            await release_job_lock(job_id, "succeeded")
            log.info(
                "fear_greed_run_complete",
                fetched=len(items),
                new_records=new_records,
            )

        except Exception as exc:
            log.error("fear_greed_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
