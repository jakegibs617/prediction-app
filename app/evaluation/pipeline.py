from __future__ import annotations

import structlog

from app.evaluation.accuracy_report import send_accuracy_report
from app.evaluation.service import evaluate_prediction, read_evaluation_candidates
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)


class EvaluationPipeline:
    async def run(self) -> None:
        job_id = await acquire_job_lock("evaluation")
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            candidates = await read_evaluation_candidates()

            counts = {"evaluated": 0, "void": 0, "not_evaluable": 0}
            for candidate in candidates:
                state = await evaluate_prediction(candidate)
                counts[state] = counts.get(state, 0) + 1

            await release_job_lock(job_id, "succeeded")
            log.info("evaluation_complete", **counts, candidates_scanned=len(candidates))

            if counts["evaluated"] > 0:
                await send_accuracy_report()
        except Exception as exc:
            log.error("evaluation_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
