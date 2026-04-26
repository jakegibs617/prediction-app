from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.alerts.rules import format_alert_payload, should_send_alert
from app.predictions.logic import PredictionRecord


def build_prediction_record(
    *,
    probability: str = "0.87",
    prediction_mode: str = "live",
    horizon_hours: int = 24,
) -> PredictionRecord:
    created_at = datetime(2026, 4, 18, 14, 1, tzinfo=UTC)
    return PredictionRecord(
        id=uuid4(),
        target_id=uuid4(),
        asset_id=uuid4(),
        feature_snapshot_id=uuid4(),
        model_version_id=uuid4(),
        prompt_version_id=None,
        prediction_mode=prediction_mode,
        predicted_outcome="up_2pct",
        probability=probability,
        evidence_summary="Price and sentiment support a short-term rebound.",
        rationale={},
        created_at=created_at,
        horizon_end_at=created_at + timedelta(hours=horizon_hours),
        correlation_id=uuid4(),
        hallucination_risk=False,
        probability_extreme_flag=False,
        context_compressed=False,
        backtest_run_id=None,
    )


def test_sends_alert_for_live_prediction() -> None:
    prediction = build_prediction_record()
    assert should_send_alert(prediction, max_horizon_hours=72)


def test_does_not_send_alert_for_long_horizon() -> None:
    prediction = build_prediction_record(horizon_hours=96)
    assert not should_send_alert(prediction, max_horizon_hours=72)


def test_does_not_send_alert_for_backtest() -> None:
    prediction = build_prediction_record(prediction_mode="backtest")
    assert not should_send_alert(prediction, max_horizon_hours=72)


def test_formats_required_alert_payload_fields() -> None:
    payload = format_alert_payload(
        build_prediction_record(),
        asset_symbol="BTC/USD",
        target_metric="price_return",
        claim_type="correlation",
    )
    assert payload["asset_symbol"] == "BTC/USD"
    assert payload["target_metric"] == "price_return"
    assert payload["probability"] == "0.87"
    assert "claim_type_warning" in payload
