from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import structlog

from app.alerts.service import process_prediction_alert, read_alert_rules
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter
from app.predictions.logic import PredictionRecord

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AlertablePrediction:
    prediction: PredictionRecord
    asset_symbol: str
    target_metric: str
    claim_type: str


async def get_alertable_predictions() -> list[AlertablePrediction]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.id,
                p.target_id,
                p.asset_id,
                p.feature_snapshot_id,
                p.model_version_id,
                p.prompt_version_id,
                p.prediction_mode,
                p.predicted_outcome,
                p.probability,
                p.llm_probability,
                p.pre_cal_probability,
                p.evidence_summary,
                p.rationale,
                p.created_at,
                p.horizon_end_at,
                p.correlation_id,
                p.hallucination_risk,
                p.probability_extreme_flag,
                p.context_compressed,
                p.backtest_run_id,
                a.symbol AS asset_symbol,
                t.target_metric,
                COALESCE(p.rationale->>'claim_type', 'correlation') AS claim_type
            FROM predictions.predictions p
            JOIN market_data.assets a ON a.id = p.asset_id
            JOIN predictions.prediction_targets t ON t.id = p.target_id
            WHERE p.prediction_mode = 'live'
              AND p.hallucination_risk = false
              AND p.probability_extreme_flag = false
            ORDER BY p.created_at DESC
            """
        )

    return [
        AlertablePrediction(
            prediction=PredictionRecord(
                id=row["id"],
                target_id=row["target_id"],
                asset_id=row["asset_id"],
                feature_snapshot_id=row["feature_snapshot_id"],
                model_version_id=row["model_version_id"],
                prompt_version_id=row["prompt_version_id"],
                prediction_mode=row["prediction_mode"],
                predicted_outcome=row["predicted_outcome"],
                probability=Decimal(row["probability"]),
                llm_probability=float(row["llm_probability"]) if row["llm_probability"] is not None else None,
                pre_cal_probability=(
                    float(row["pre_cal_probability"]) if row["pre_cal_probability"] is not None else None
                ),
                evidence_summary=row["evidence_summary"],
                rationale=row["rationale"],
                created_at=row["created_at"],
                horizon_end_at=row["horizon_end_at"],
                correlation_id=row["correlation_id"],
                hallucination_risk=row["hallucination_risk"],
                probability_extreme_flag=row["probability_extreme_flag"],
                context_compressed=row["context_compressed"],
                backtest_run_id=row["backtest_run_id"],
            ),
            asset_symbol=row["asset_symbol"],
            target_metric=row["target_metric"],
            claim_type=row["claim_type"],
        )
        for row in rows
    ]


class AlertCheckPipeline:
    async def run(self) -> None:
        job_id = await acquire_job_lock("alert_check")
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            rules = await read_alert_rules()
            predictions = await get_alertable_predictions()

            total_predictions = 0
            total_deliveries = 0

            for item in predictions:
                total_predictions += 1
                results = await process_prediction_alert(
                    item.prediction,
                    asset_symbol=item.asset_symbol,
                    target_metric=item.target_metric,
                    claim_type=item.claim_type,
                    rules=rules,
                )
                total_deliveries += len(results)

            await release_job_lock(job_id, "succeeded")
            log.info(
                "alert_check_complete",
                predictions_scanned=total_predictions,
                deliveries_attempted=total_deliveries,
            )
        except Exception as exc:
            log.error("alert_check_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
