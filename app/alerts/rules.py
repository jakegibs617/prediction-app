from __future__ import annotations

from app.predictions.logic import PredictionRecord


def should_send_alert(
    prediction: PredictionRecord,
    *,
    min_probability: float,
    max_horizon_hours: int,
) -> bool:
    horizon_hours = (prediction.horizon_end_at - prediction.created_at).total_seconds() / 3600
    return (
        prediction.prediction_mode == "live"
        and float(prediction.probability) >= min_probability
        and horizon_hours <= max_horizon_hours
        and not prediction.hallucination_risk
        and not prediction.probability_extreme_flag
    )


def format_alert_payload(
    prediction: PredictionRecord,
    *,
    asset_symbol: str,
    target_metric: str,
    claim_type: str,
) -> dict[str, str]:
    evidence_summary = prediction.evidence_summary[:500]
    horizon_hours = int((prediction.horizon_end_at - prediction.created_at).total_seconds() // 3600)
    payload = {
        "prediction_id": str(prediction.id),
        "created_at": prediction.created_at.isoformat(),
        "asset_symbol": asset_symbol,
        "target_metric": target_metric,
        "forecast_horizon_hours": str(horizon_hours),
        "predicted_outcome": prediction.predicted_outcome,
        "probability": f"{float(prediction.probability):.2f}",
        "evidence_summary": evidence_summary,
    }
    if claim_type == "correlation":
        payload["claim_type_warning"] = "Correlation only - causation not established"
    return payload
