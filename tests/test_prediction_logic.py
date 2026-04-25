from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.predictions.contracts import (
    DirectionRule,
    FeatureSnapshot,
    FeatureValue,
    PredictionInput,
    PredictionTarget,
    SettlementRule,
)
from app.predictions.logic import build_prediction_record, validate_no_future_leak


def build_snapshot(*, available_at: datetime | None = None) -> FeatureSnapshot:
    created_at = datetime(2026, 4, 18, 14, 0, tzinfo=UTC)
    return FeatureSnapshot(
        snapshot_id=uuid4(),
        asset_id=(asset_id := uuid4()),
        asset_symbol="BTC/USD",
        as_of_at=created_at,
        feature_set_name="baseline-v1",
        values=[
            FeatureValue(
                feature_key="price_return_24h",
                feature_type="numeric",
                numeric_value=-0.0231,
                available_at=available_at or created_at - timedelta(minutes=1),
                source_record_ids=[uuid4()],
            )
        ],
    )


def build_target() -> PredictionTarget:
    return PredictionTarget(
        id=uuid4(),
        name="btc_up_2pct_24h",
        asset_type="crypto",
        target_metric="price_return",
        horizon_hours=24,
        direction_rule=DirectionRule(
            direction="up",
            metric="price_return",
            threshold=0.02,
            unit="fraction",
        ),
        settlement_rule=SettlementRule(
            type="continuous",
            horizon="wall_clock_hours",
            n=24,
            calendar="none",
        ),
    )


def build_prediction_input(*, probability: float = 0.87, available_at: datetime | None = None) -> PredictionInput:
    created_at = datetime(2026, 4, 18, 14, 1, tzinfo=UTC)
    snapshot = build_snapshot(available_at=available_at)
    return PredictionInput(
        target=build_target(),
        asset_id=snapshot.asset_id,
        asset_symbol="BTC/USD",
        asset_type="crypto",
        feature_snapshot=snapshot,
        model_version_id=uuid4(),
        probability=probability,
        evidence_summary="Mean reversion setup with mild positive sentiment.",
        predicted_outcome="up_2pct",
        prediction_mode="live",
        created_at=created_at,
        correlation_id=uuid4(),
    )


def test_rejects_probability_above_one() -> None:
    with pytest.raises(ValidationError):
        build_prediction_input(probability=1.01)


def test_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        PredictionInput.model_validate({})


def test_attaches_horizon_from_target() -> None:
    prediction = build_prediction_record(build_prediction_input())
    assert prediction.horizon_end_at == datetime(2026, 4, 19, 14, 1, tzinfo=UTC)


def test_blocks_predictions_with_future_features() -> None:
    with pytest.raises(ValueError, match="future data"):
        validate_no_future_leak(
            build_snapshot(available_at=datetime(2026, 4, 18, 14, 1, tzinfo=UTC)),
            datetime(2026, 4, 18, 14, 1, tzinfo=UTC),
        )


def test_build_prediction_record_is_immutable_dataclass() -> None:
    prediction = build_prediction_record(build_prediction_input())
    with pytest.raises(Exception):
        prediction.probability = 0.5
