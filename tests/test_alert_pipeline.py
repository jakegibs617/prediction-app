from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.alerts.pipeline import AlertCheckPipeline, get_alertable_predictions


class FakeConnection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self.rows


class FakeAcquire:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_get_alertable_predictions_maps_rows(monkeypatch) -> None:
    prediction_id = uuid4()
    created_at = datetime(2026, 4, 18, 14, 1, tzinfo=UTC)
    conn = FakeConnection(
        rows=[
            {
                "id": prediction_id,
                "target_id": uuid4(),
                "asset_id": uuid4(),
                "feature_snapshot_id": uuid4(),
                "model_version_id": uuid4(),
                "prompt_version_id": None,
                "prediction_mode": "live",
                "predicted_outcome": "up_2pct",
                "probability": Decimal("0.87"),
                "evidence_summary": "Evidence",
                "rationale": {"claim_type": "correlation"},
                "created_at": created_at,
                "horizon_end_at": created_at + timedelta(hours=24),
                "correlation_id": uuid4(),
                "hallucination_risk": False,
                "probability_extreme_flag": False,
                "context_compressed": False,
                "backtest_run_id": None,
                "asset_symbol": "BTC/USD",
                "target_metric": "price_return",
                "claim_type": "correlation",
            }
        ]
    )
    monkeypatch.setattr("app.alerts.pipeline.get_pool", lambda: FakePool(conn))

    items = await get_alertable_predictions()

    assert len(items) == 1
    assert items[0].prediction.id == prediction_id
    assert items[0].asset_symbol == "BTC/USD"
    assert any("FROM predictions.predictions p" in query for query, _ in conn.fetch_calls)


@pytest.mark.asyncio
async def test_alert_check_pipeline_processes_predictions(monkeypatch) -> None:
    job_calls: list[str] = []
    processed: list[tuple[str, str]] = []

    async def fake_acquire_job_lock(*args, **kwargs):
        job_calls.append("acquire")
        return uuid4()

    async def fake_increment_attempt(*args, **kwargs):
        job_calls.append("increment")
        return 1

    async def fake_release_job_lock(*args, **kwargs):
        job_calls.append("release")
        return None

    async def fake_send_to_dead_letter(*args, **kwargs):
        job_calls.append("dead_letter")
        return None

    async def fake_read_alert_rules():
        return ["rule"]

    async def fake_get_alertable_predictions():
        created_at = datetime(2026, 4, 18, 14, 1, tzinfo=UTC)
        from app.alerts.pipeline import AlertablePrediction
        from app.predictions.logic import PredictionRecord

        return [
            AlertablePrediction(
                prediction=PredictionRecord(
                    id=uuid4(),
                    target_id=uuid4(),
                    asset_id=uuid4(),
                    feature_snapshot_id=uuid4(),
                    model_version_id=uuid4(),
                    prompt_version_id=None,
                    prediction_mode="live",
                    predicted_outcome="up_2pct",
                    probability=Decimal("0.87"),
                    evidence_summary="Evidence",
                    rationale={},
                    created_at=created_at,
                    horizon_end_at=created_at + timedelta(hours=24),
                    correlation_id=uuid4(),
                    hallucination_risk=False,
                    probability_extreme_flag=False,
                    context_compressed=False,
                    backtest_run_id=None,
                ),
                asset_symbol="BTC/USD",
                target_metric="price_return",
                claim_type="correlation",
            )
        ]

    async def fake_process_prediction_alert(prediction, *, asset_symbol: str, target_metric: str, claim_type: str, rules):
        processed.append((asset_symbol, target_metric))
        return ["sent"]

    monkeypatch.setattr("app.alerts.pipeline.acquire_job_lock", fake_acquire_job_lock)
    monkeypatch.setattr("app.alerts.pipeline.increment_attempt", fake_increment_attempt)
    monkeypatch.setattr("app.alerts.pipeline.release_job_lock", fake_release_job_lock)
    monkeypatch.setattr("app.alerts.pipeline.send_to_dead_letter", fake_send_to_dead_letter)
    monkeypatch.setattr("app.alerts.pipeline.read_alert_rules", fake_read_alert_rules)
    monkeypatch.setattr("app.alerts.pipeline.get_alertable_predictions", fake_get_alertable_predictions)
    monkeypatch.setattr("app.alerts.pipeline.process_prediction_alert", fake_process_prediction_alert)

    await AlertCheckPipeline().run()

    assert processed == [("BTC/USD", "price_return")]
    assert job_calls == ["acquire", "increment", "release"]
