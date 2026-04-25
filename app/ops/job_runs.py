from uuid import UUID, uuid4

import json

import structlog

from app.db.pool import get_pool
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

# Valid terminal statuses per ops.job_runs check constraint
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_DEAD_LETTER = "dead_letter"


async def acquire_job_lock(
    job_name: str,
    source_name: str | None = None,
    correlation_id: UUID | None = None,
    max_attempts: int = 3,
) -> UUID | None:
    """
    Insert a new job_run row in 'running' status.
    Returns the new job_run UUID, or None if an identical job is already running.
    source_name is stored in the metadata jsonb column.
    """
    pool = get_pool()
    job_id = uuid4()
    cid = correlation_id or uuid4()
    metadata = {"source_name": source_name} if source_name else {}

    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT id FROM ops.job_runs
            WHERE job_name = $1
              AND status = 'running'
              AND ($2::text IS NULL OR metadata->>'source_name' = $2)
            LIMIT 1
            """,
            job_name, source_name,
        )
        if existing:
            log.info("job_already_running", job_name=job_name, source_name=source_name, existing_id=str(existing))
            return None

        await conn.execute(
            """
            INSERT INTO ops.job_runs
                (id, job_name, status, correlation_id, started_at, attempt_count, max_attempts, metadata)
            VALUES ($1, $2, 'running', $3, $4, 0, $5, $6)
            """,
            job_id, job_name, cid, get_utc_now(), max_attempts,
            json.dumps(metadata),
        )

    log.info("job_acquired", job_id=str(job_id), job_name=job_name, source_name=source_name)
    return job_id


async def release_job_lock(
    job_id: UUID,
    status: str,
    error_summary: str | None = None,
) -> None:
    """Update job_run to a terminal status ('succeeded' | 'failed' | 'dead_letter')."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ops.job_runs
            SET status = $1, finished_at = $2, error_summary = $3
            WHERE id = $4
            """,
            status, get_utc_now(), error_summary, job_id,
        )
    log.info("job_released", job_id=str(job_id), status=status)


async def increment_attempt(job_id: UUID) -> int:
    """Bump attempt_count and return the new value."""
    pool = get_pool()
    async with pool.acquire() as conn:
        new_count = await conn.fetchval(
            """
            UPDATE ops.job_runs
            SET attempt_count = attempt_count + 1
            WHERE id = $1
            RETURNING attempt_count
            """,
            job_id,
        )
    return new_count


async def send_to_dead_letter(job_id: UUID, reason: str) -> None:
    """Mark a job as dead_letter after exhausting retry budget."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ops.job_runs
            SET status = 'dead_letter', finished_at = $1, error_summary = $2
            WHERE id = $3
            """,
            get_utc_now(), reason, job_id,
        )
    log.warning("job_dead_letter", job_id=str(job_id), reason=reason)
