"""Run the full pipeline once (post-normalization) and report counts at each stage."""
import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.alerts.pipeline import AlertCheckPipeline  # noqa: E402
from app.db.pool import close_pool, get_pool, init_pool  # noqa: E402
from app.evaluation.pipeline import EvaluationPipeline  # noqa: E402
from app.features.pipeline import FeaturePipeline  # noqa: E402
from app.predictions.pipeline import PredictionPipeline  # noqa: E402


async def counts(label: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT count(*) FROM ingestion.raw_source_records")
        norm = await conn.fetchval("SELECT count(*) FROM ingestion.normalized_events")
        feats = await conn.fetchval("SELECT count(*) FROM features.feature_snapshots")
        preds = await conn.fetchval("SELECT count(*) FROM predictions.predictions")
        evals = await conn.fetchval("SELECT count(*) FROM evaluation.evaluation_results")
        deliveries = await conn.fetchval("SELECT count(*) FROM ops.alert_deliveries")
    print(
        f"=== {label} === raw={raw} norm={norm} features={feats} "
        f"predictions={preds} evals={evals} alerts={deliveries}"
    )


async def main() -> int:
    await init_pool()
    try:
        await counts("BEFORE")

        # Clear any lingering "running" job locks first.
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ops.job_runs SET status = 'failed', finished_at = NOW(), "
                "error_summary = 'cleared stale lock' WHERE status = 'running'"
            )

        print("\n>>> stage: feature_generation")
        await FeaturePipeline().run()
        await counts("AFTER feature_generation")

        print("\n>>> stage: prediction_run")
        await PredictionPipeline().run()
        await counts("AFTER prediction_run")

        print("\n>>> stage: alert_check")
        await AlertCheckPipeline().run()
        await counts("AFTER alert_check")

        print("\n>>> stage: evaluation")
        await EvaluationPipeline().run()
        await counts("AFTER evaluation")
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
