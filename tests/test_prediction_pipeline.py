from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.predictions.pipeline import PredictionPipeline
from app.predictions.service import PredictionCandidate
from app.predictions.contracts import PredictionTarget, DirectionRule, SettlementRule


def build_candidate() -> PredictionCandidate:
    from app.features.engine import PriceBar, build_price_feature_snapshot

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
    return PredictionCandidate(
        target=PredictionTarget(
            id=uuid4(),
            name="btc_up_2pct_24h",
            asset_type="crypto",
            target_metric="price_return",
            horizon_hours=24,
            direction_rule=DirectionRule(direction="up", metric="price_return", threshold=0.02, unit="fraction"),
            settlement_rule=SettlementRule(type="continuous", horizon="wall_clock_hours", n=24, calendar="none"),
        ),
        snapshot=snapshot,
        asset_type="crypto",
    )


@pytest.mark.asyncio
async def test_prediction_pipeline_runs_candidates(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_acquire(*args, **kwargs):
        calls.append("acquire")
        return uuid4()

    async def fake_increment(*args, **kwargs):
        calls.append("increment")
        return 1

    async def fake_release(*args, **kwargs):
        calls.append("release")
        return None

    async def fake_dead_letter(*args, **kwargs):
        calls.append("dead_letter")
        return None

    async def fake_read_candidates():
        return [build_candidate()]

    async def fake_generate(candidate, *, correlation_id):
        calls.append("generate")
        return object()

    monkeypatch.setattr("app.predictions.pipeline.acquire_job_lock", fake_acquire)
    monkeypatch.setattr("app.predictions.pipeline.increment_attempt", fake_increment)
    monkeypatch.setattr("app.predictions.pipeline.release_job_lock", fake_release)
    monkeypatch.setattr("app.predictions.pipeline.send_to_dead_letter", fake_dead_letter)
    monkeypatch.setattr("app.predictions.pipeline.read_prediction_candidates", fake_read_candidates)
    monkeypatch.setattr("app.predictions.pipeline.generate_prediction_for_candidate", fake_generate)

    await PredictionPipeline().run()

    assert calls[0:2] == ["acquire", "increment"]
    assert "generate" in calls
    assert calls[-1] == "release"
