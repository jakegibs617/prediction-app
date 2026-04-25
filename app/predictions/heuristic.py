from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.predictions.contracts import FeatureSnapshot, PredictionInput, PredictionTarget


HEURISTIC_MODEL_NAME = "heuristic-baseline"
HEURISTIC_MODEL_VERSION = "v1.0"


def _feature_map(snapshot: FeatureSnapshot) -> dict[str, float]:
    return {
        value.feature_key: value.numeric_value
        for value in snapshot.values
        if value.numeric_value is not None
    }


def _clamp_probability(probability: float) -> float:
    return max(0.0, min(1.0, round(probability, 5)))


def generate_heuristic_prediction_input(
    *,
    target: PredictionTarget,
    snapshot: FeatureSnapshot,
    asset_type: str,
    model_version_id: UUID,
    created_at: datetime,
    correlation_id: UUID,
    prompt_version_id: UUID | None = None,
    prediction_mode: str = "live",
) -> PredictionInput:
    features = _feature_map(snapshot)
    day_return = features.get("price_return_24h", 0.0)
    hour_return = features.get("price_return_1h", 0.0)
    threshold = target.direction_rule.threshold or 0.0
    direction = target.direction_rule.direction

    if direction == "up":
        signal_strength = max(0.0, (-day_return) + max(0.0, -hour_return * 0.5))
        probability = 0.50 + min(0.35, signal_strength * 3)
        predicted_outcome = f"up_{int(threshold * 100)}pct" if threshold else "up"
    elif direction == "down":
        signal_strength = max(0.0, day_return + max(0.0, hour_return * 0.5))
        probability = 0.50 + min(0.35, signal_strength * 3)
        predicted_outcome = f"down_{int(threshold * 100)}pct" if threshold else "down"
    else:
        dispersion = abs(day_return) + abs(hour_return)
        probability = 0.50 + max(0.0, 0.20 - dispersion)
        predicted_outcome = "neutral"

    evidence_parts = [
        f"24h price return is {day_return:.2%}.",
        f"1h price return is {hour_return:.2%}.",
        "Baseline heuristic uses price mean reversion only.",
        "Correlation only - causation not established.",
    ]

    return PredictionInput(
        target=target,
        asset_id=snapshot.asset_id,
        asset_symbol=snapshot.asset_symbol,
        asset_type=asset_type,
        feature_snapshot=snapshot,
        model_version_id=model_version_id,
        prompt_version_id=prompt_version_id,
        probability=_clamp_probability(probability),
        evidence_summary=" ".join(evidence_parts),
        predicted_outcome=predicted_outcome,
        prediction_mode=prediction_mode,
        created_at=created_at,
        correlation_id=correlation_id,
        rationale={
            "feature_count": len(snapshot.values),
            "features_omitted": 0,
            "compression_type": None,
            "evidence_grounding_ok": True,
            "model_name": HEURISTIC_MODEL_NAME,
            "model_version": HEURISTIC_MODEL_VERSION,
        },
        claim_type="correlation",
    )
