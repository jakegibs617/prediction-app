from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.evaluation.pipeline import EvaluationPipeline
from app.evaluation.service import EvaluationCandidate
from app.predictions.contracts import DirectionRule, SettlementRule


@pytest.mark.asyncio
async def test_evaluation_pipeline_runs_candidates(monkeypatch) -> None:
    calls: list[str] = []
    candidate = EvaluationCandidate(
        prediction_id=uuid4(),
        asset_id=uuid4(),
        created_at=datetime(2026, 4, 18, 14, 1, tzinfo=UTC),
        horizon_end_at=datetime(2026, 4, 19, 14, 1, tzinfo=UTC),
        probability=Decimal("0.87"),
        predicted_outcome="up_2pct",
        target_metric="price_return",
        asset_type="crypto",
        direction_rule=DirectionRule(direction="up", metric="price_return", threshold=0.02, unit="fraction"),
        settlement_rule=SettlementRule(type="continuous", horizon="wall_clock_hours", n=24, calendar="none"),
    )

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

    async def fake_read():
        return [candidate]

    async def fake_evaluate(item):
        calls.append(f"evaluate:{item.prediction_id}")
        return "evaluated"

    async def fake_accuracy_report():
        calls.append("accuracy_report")

    monkeypatch.setattr("app.evaluation.pipeline.acquire_job_lock", fake_acquire)
    monkeypatch.setattr("app.evaluation.pipeline.increment_attempt", fake_increment)
    monkeypatch.setattr("app.evaluation.pipeline.release_job_lock", fake_release)
    monkeypatch.setattr("app.evaluation.pipeline.send_to_dead_letter", fake_dead_letter)
    monkeypatch.setattr("app.evaluation.pipeline.read_evaluation_candidates", fake_read)
    monkeypatch.setattr("app.evaluation.pipeline.evaluate_prediction", fake_evaluate)
    monkeypatch.setattr("app.evaluation.pipeline.send_accuracy_report", fake_accuracy_report)

    await EvaluationPipeline().run()

    assert calls[0:2] == ["acquire", "increment"]
    assert any(call.startswith("evaluate:") for call in calls)
    assert "accuracy_report" in calls
    assert "release" in calls
