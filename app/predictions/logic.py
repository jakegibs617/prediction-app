from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.config import settings
from app.predictions.contracts import FeatureSnapshot, PredictionInput


@dataclass(frozen=True)
class PredictionRecord:
    id: UUID
    target_id: UUID
    asset_id: UUID
    feature_snapshot_id: UUID
    model_version_id: UUID
    prompt_version_id: UUID | None
    prediction_mode: str
    predicted_outcome: str
    probability: Decimal
    llm_probability: float | None
    pre_cal_probability: float | None
    evidence_summary: str
    rationale: dict
    created_at: datetime
    horizon_end_at: datetime
    correlation_id: UUID
    hallucination_risk: bool
    probability_extreme_flag: bool
    context_compressed: bool
    backtest_run_id: UUID | None


def validate_no_future_leak(snapshot: FeatureSnapshot, issuance_time: datetime) -> None:
    if snapshot.as_of_at > issuance_time:
        raise ValueError("feature snapshot as_of_at must be <= prediction issuance time")

    leaking_features = [
        value.feature_key
        for value in snapshot.values
        if value.available_at >= issuance_time
    ]
    if leaking_features:
        joined = ", ".join(sorted(leaking_features))
        raise ValueError(f"feature snapshot contains future data: {joined}")


def build_prediction_record(
    prediction_input: PredictionInput,
    *,
    llm_probability: float | None = None,
    pre_cal_probability: float | None = None,
) -> PredictionRecord:
    validate_no_future_leak(prediction_input.feature_snapshot, prediction_input.created_at)

    if len(prediction_input.evidence_summary) > settings.max_evidence_summary_chars:
        raise ValueError("evidence_summary exceeds configured maximum length")

    return PredictionRecord(
        id=uuid4(),
        target_id=prediction_input.target.id,
        asset_id=prediction_input.asset_id,
        feature_snapshot_id=prediction_input.feature_snapshot.snapshot_id,
        model_version_id=prediction_input.model_version_id,
        prompt_version_id=prediction_input.prompt_version_id,
        prediction_mode=prediction_input.prediction_mode,
        predicted_outcome=prediction_input.predicted_outcome,
        probability=Decimal(f"{prediction_input.probability:.5f}"),
        llm_probability=llm_probability,
        pre_cal_probability=pre_cal_probability,
        evidence_summary=prediction_input.evidence_summary,
        rationale={
            **prediction_input.rationale,
            "claim_type": prediction_input.claim_type,
            "context_compressed": prediction_input.context_compressed,
        },
        created_at=prediction_input.created_at,
        horizon_end_at=prediction_input.horizon_end_at,
        correlation_id=prediction_input.correlation_id,
        hallucination_risk=prediction_input.hallucination_risk,
        probability_extreme_flag=prediction_input.probability_extreme_flag,
        context_compressed=prediction_input.context_compressed,
        backtest_run_id=prediction_input.backtest_run_id,
    )
