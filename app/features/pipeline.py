from __future__ import annotations

import structlog

from app.features.service import generate_features_for_asset, read_feature_candidates
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)


class FeaturePipeline:
    async def run(self) -> None:
        job_id = await acquire_job_lock("feature_generation")
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            candidates = await read_feature_candidates()

            created_count = 0
            skipped_count = 0
            for candidate in candidates:
                snapshot = await generate_features_for_asset(candidate)
                if snapshot is None:
                    skipped_count += 1
                else:
                    created_count += 1

            await release_job_lock(job_id, "succeeded")
            log.info(
                "feature_generation_complete",
                candidates_scanned=len(candidates),
                snapshots_created=created_count,
                snapshots_skipped=skipped_count,
            )
        except Exception as exc:
            log.error("feature_generation_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
