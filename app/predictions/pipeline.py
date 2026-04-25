from __future__ import annotations

from uuid import uuid4

import structlog

from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter
from app.predictions.service import generate_prediction_for_candidate, read_prediction_candidates

log = structlog.get_logger(__name__)


class PredictionPipeline:
    async def run(self) -> None:
        correlation_id = uuid4()
        job_id = await acquire_job_lock("prediction_run", correlation_id=correlation_id)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            candidates = await read_prediction_candidates()

            created_count = 0
            skipped_count = 0
            for candidate in candidates:
                record = await generate_prediction_for_candidate(candidate, correlation_id=correlation_id)
                if record is None:
                    skipped_count += 1
                else:
                    created_count += 1

            await release_job_lock(job_id, "succeeded")
            log.info(
                "prediction_run_complete",
                candidates_scanned=len(candidates),
                predictions_created=created_count,
                predictions_skipped=skipped_count,
                correlation_id=str(correlation_id),
            )
        except Exception as exc:
            log.error("prediction_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
