from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.features.engine import PriceBar, build_price_feature_snapshot
from app.predictions.heuristic import generate_heuristic_prediction_input
from app.predictions.logic import build_prediction_record
from app.predictions.contracts import DirectionRule, PredictionTarget, SettlementRule


def build_snapshot() -> tuple:
    asset_id = uuid4()
    start = datetime(2026, 4, 17, 0, 0, tzinfo=UTC)
    bars = [
        PriceBar(
            asset_id=asset_id,
            source_record_id=uuid4(),
            bar_start_at=start + timedelta(hours=index),
            bar_end_at=start + timedelta(hours=index, minutes=59),
            close=120 - index,
        )
        for index in range(30)
    ]
    snapshot = build_price_feature_snapshot(
        asset_id=asset_id,
        asset_symbol="BTC/USD",
        as_of_at=datetime(2026, 4, 18, 6, 0, tzinfo=UTC),
        price_bars=bars,
    )
    return asset_id, snapshot


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


def test_generate_heuristic_prediction_is_deterministic() -> None:
    _, snapshot = build_snapshot()
    prediction_input = generate_heuristic_prediction_input(
        target=build_target(),
        snapshot=snapshot,
        asset_type="crypto",
        model_version_id=uuid4(),
        created_at=datetime(2026, 4, 18, 6, 1, tzinfo=UTC),
        correlation_id=uuid4(),
    )

    assert prediction_input.prediction_mode == "live"
    assert prediction_input.claim_type == "correlation"
    assert prediction_input.probability >= 0.5
    assert "Correlation only" in prediction_input.evidence_summary


def test_generate_heuristic_prediction_builds_valid_record() -> None:
    _, snapshot = build_snapshot()
    prediction_record = build_prediction_record(
        generate_heuristic_prediction_input(
            target=build_target(),
            snapshot=snapshot,
            asset_type="crypto",
            model_version_id=uuid4(),
            created_at=datetime(2026, 4, 18, 6, 1, tzinfo=UTC),
            correlation_id=uuid4(),
            prediction_mode="backtest",
        )
    )

    assert prediction_record.prediction_mode == "backtest"
    assert prediction_record.horizon_end_at == datetime(2026, 4, 19, 6, 1, tzinfo=UTC)
