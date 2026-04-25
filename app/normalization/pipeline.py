"""
Normalization pipeline — reads pending raw_source_records and routes them:
  - market_data / macro  → mark validated immediately (no LLM needed)
  - news / events        → LLM extraction → write normalized_events → mark validated

Run once per scheduler tick via NormalizationPipeline().run().
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

from app.config import settings
from app.db.pool import get_pool
from app.model_client.factory import get_cheap_model_client
from app.normalization.extractor import extract_event_metadata
from app.ops.job_runs import (
    acquire_job_lock,
    increment_attempt,
    release_job_lock,
    send_to_dead_letter,
)

log = structlog.get_logger(__name__)

_TEXT_FIELDS = ("title", "headline", "description", "content", "text", "body", "summary")
_DATE_FIELDS = ("publishedAt", "published_at", "date", "timestamp", "event_at")

# Categories that produce text for LLM extraction
_LLM_CATEGORIES = frozenset({"news", "events"})
# Categories that carry structured numeric data — validated as-is, no LLM
_SKIP_CATEGORIES = frozenset({"market_data", "macro"})


def _extract_text(payload: dict) -> str | None:
    parts = [
        str(payload[field])
        for field in _TEXT_FIELDS
        if payload.get(field) and isinstance(payload[field], str)
    ]
    return " ".join(parts) if parts else None


def _extract_event_time(payload: dict, fallback: datetime | None) -> datetime | None:
    for field in _DATE_FIELDS:
        value = payload.get(field)
        if not value:
            continue
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
                try:
                    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return fallback


def _validate_raw_payload(payload: object, category: str) -> list[str]:
    if not isinstance(payload, dict):
        return ["raw_payload must be a JSON object"]
    if not payload:
        return ["raw_payload must not be empty"]

    if category in _SKIP_CATEGORIES:
        required_keys = ("value", "close", "open", "high", "low", "date", "series_id", "indicator_code")
        if not any(payload.get(key) not in (None, "", []) for key in required_keys):
            return ["structured source payload is missing required fields"]

    return []


class NormalizationPipeline:
    async def run(self) -> None:
        job_id = await acquire_job_lock("normalization")
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()
            model_client = get_cheap_model_client()
            batch_size = settings.normalization_batch_size

            total_validated = 0
            total_extracted = 0
            total_failed = 0

            while True:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT rsr.id, rsr.raw_payload, rsr.source_id, rsr.source_recorded_at,
                               s.category
                        FROM ingestion.raw_source_records rsr
                        JOIN ops.api_sources s ON s.id = rsr.source_id
                        WHERE rsr.validation_status = 'pending'
                        ORDER BY rsr.ingested_at ASC
                        LIMIT $1
                        """,
                        batch_size,
                    )

                if not rows:
                    break

                for row in rows:
                    record_id = row["id"]
                    category = row["category"]
                    raw_payload = row["raw_payload"]

                    if isinstance(raw_payload, str):
                        try:
                            raw_payload = json.loads(raw_payload)
                        except json.JSONDecodeError:
                            async with pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE ingestion.raw_source_records"
                                    " SET validation_status = 'rejected',"
                                    "     validation_errors = $1"
                                    " WHERE id = $2",
                                    json.dumps(["raw_payload is not valid JSON"]),
                                    record_id,
                                )
                                total_failed += 1
                            continue

                    validation_errors = _validate_raw_payload(raw_payload, category)
                    if validation_errors:
                        async with pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE ingestion.raw_source_records"
                                " SET validation_status = 'rejected',"
                                "     validation_errors = $1"
                                " WHERE id = $2",
                                json.dumps(validation_errors),
                                record_id,
                            )
                            total_failed += 1
                        continue

                    async with pool.acquire() as conn:
                        if category in _SKIP_CATEGORIES:
                            await conn.execute(
                                "UPDATE ingestion.raw_source_records"
                                " SET validation_status = 'valid' WHERE id = $1",
                                record_id,
                            )
                            total_validated += 1

                        elif category in _LLM_CATEGORIES:
                            await self._process_text_record(
                                conn=conn,
                                record_id=record_id,
                                category=category,
                                raw_payload=raw_payload,
                                source_recorded_at=row["source_recorded_at"],
                                model_client=model_client,
                            )
                            # Determine outcome from updated status
                            status = await conn.fetchval(
                                "SELECT validation_status FROM ingestion.raw_source_records WHERE id = $1",
                                record_id,
                            )
                            if status == "valid":
                                total_extracted += 1
                            else:
                                total_failed += 1

                        else:
                            # Unknown categories are rejected explicitly instead of silently passing through.
                            log.warning("unknown_source_category", category=category, record_id=str(record_id))
                            await conn.execute(
                                "UPDATE ingestion.raw_source_records"
                                " SET validation_status = 'rejected',"
                                "     validation_errors = $1"
                                " WHERE id = $2",
                                json.dumps([f"unsupported source category: {category}"]),
                                record_id,
                            )
                            total_failed += 1

            await release_job_lock(job_id, "succeeded")
            log.info(
                "normalization_run_complete",
                validated=total_validated,
                extracted=total_extracted,
                failed=total_failed,
            )

        except Exception as exc:
            log.error("normalization_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise

    async def _process_text_record(
        self,
        conn,
        *,
        record_id,
        category: str,
        raw_payload: dict,
        source_recorded_at: datetime | None,
        model_client,
    ) -> None:
        text = _extract_text(raw_payload)
        if not text:
            log.warning("no_extractable_text", record_id=str(record_id))
            await conn.execute(
                "UPDATE ingestion.raw_source_records"
                " SET validation_status = 'rejected',"
                "     validation_errors = $1"
                " WHERE id = $2",
                json.dumps(["no extractable text fields in raw_payload"]),
                record_id,
            )
            return

        try:
            result = await extract_event_metadata(text, category, model_client)
            event_at = _extract_event_time(raw_payload, source_recorded_at)

            await conn.execute(
                """
                INSERT INTO ingestion.normalized_events
                    (source_record_id, event_type, event_subtype, title, summary,
                     sentiment_score, severity_score, country_code, region,
                     entity_data, event_occurred_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT DO NOTHING
                """,
                record_id,
                result.event_type,
                result.event_subtype,
                result.title,
                result.summary,
                str(result.sentiment_score),
                str(result.severity_score),
                result.country_code,
                result.region,
                json.dumps(result.entities.model_dump()),
                event_at,
            )
            await conn.execute(
                "UPDATE ingestion.raw_source_records"
                " SET validation_status = 'valid' WHERE id = $1",
                record_id,
            )

        except Exception as exc:
            log.error(
                "extraction_failed",
                record_id=str(record_id),
                category=category,
                error=str(exc),
            )
            await conn.execute(
                "UPDATE ingestion.raw_source_records"
                " SET validation_status = 'quarantined',"
                "     validation_errors = $1"
                " WHERE id = $2",
                json.dumps([str(exc)]),
                record_id,
            )
