from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.evaluation.service import (
    EvaluationCandidate,
    build_settlement_time,
    compute_actual_outcome,
    evaluate_prediction,
    read_evaluation_candidates,
    write_evaluation_result,
)


class FakeConnection:
    def __init__(self, rows: list[dict] | None = None, fetchrow_results: list[dict | None] | None = None, fetchval_results: list[object] | None = None) -> None:
        self.rows = rows or []
        self.fetchrow_results = fetchrow_results or []
        self.fetchval_results = fetchval_results or []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        return self.fetchval_results.pop(0) if self.fetchval_results else uuid4()


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


def build_candidate(*, asset_type: str = "crypto", settlement_type: str = "continuous") -> EvaluationCandidate:
    created_at = datetime(2026, 4, 18, 14, 1, tzinfo=UTC)
    return EvaluationCandidate(
        prediction_id=uuid4(),
        asset_id=uuid4(),
        created_at=created_at,
        horizon_end_at=created_at + timedelta(hours=24),
        probability=Decimal("0.87"),
        predicted_outcome="up_2pct",
        target_metric="price_return",
        asset_type=asset_type,
        direction_rule={"direction": "up", "metric": "price_return", "threshold": 0.02, "unit": "fraction"},  # type: ignore[arg-type]
        settlement_rule={"type": settlement_type, "horizon": "wall_clock_hours", "n": 24, "calendar": "none" if settlement_type == "continuous" else "NYSE"},  # type: ignore[arg-type]
    )


def normalized_candidate(candidate: EvaluationCandidate) -> EvaluationCandidate:
    from app.predictions.contracts import DirectionRule, SettlementRule

    return EvaluationCandidate(
        **{
            **candidate.__dict__,
            "direction_rule": DirectionRule.model_validate(candidate.direction_rule),
            "settlement_rule": SettlementRule.model_validate(candidate.settlement_rule),
        }
    )


@pytest.mark.asyncio
async def test_read_evaluation_candidates_parses_json_rules(monkeypatch) -> None:
    conn = FakeConnection(
        rows=[
            {
                "prediction_id": uuid4(),
                "asset_id": uuid4(),
                "created_at": datetime(2026, 4, 18, 14, 1, tzinfo=UTC),
                "horizon_end_at": datetime(2026, 4, 19, 14, 1, tzinfo=UTC),
                "probability": Decimal("0.87"),
                "predicted_outcome": "up_2pct",
                "target_metric": "price_return",
                "asset_type": "crypto",
                "direction_rule": json.dumps({"direction": "up", "metric": "price_return", "threshold": 0.02, "unit": "fraction"}),
                "settlement_rule": json.dumps({"type": "continuous", "horizon": "wall_clock_hours", "n": 24, "calendar": "none"}),
            }
        ]
    )
    monkeypatch.setattr("app.evaluation.service.get_pool", lambda: FakePool(conn))

    candidates = await read_evaluation_candidates()

    assert len(candidates) == 1
    assert candidates[0].direction_rule.direction == "up"


def test_build_settlement_time_uses_next_trading_day_close_for_equity() -> None:
    candidate = normalized_candidate(build_candidate(asset_type="equity", settlement_type="trading_day_close"))
    settlement_time = build_settlement_time(candidate)
    assert settlement_time.weekday() == 0
    assert settlement_time.hour == 16


def test_compute_actual_outcome_uses_threshold() -> None:
    candidate = normalized_candidate(build_candidate())
    assert compute_actual_outcome(candidate, 0.03)
    assert not compute_actual_outcome(candidate, 0.01)


@pytest.mark.asyncio
async def test_write_evaluation_result_uses_upsert(monkeypatch) -> None:
    conn = FakeConnection(fetchval_results=[uuid4()])
    monkeypatch.setattr("app.evaluation.service.get_pool", lambda: FakePool(conn))

    await write_evaluation_result(prediction_id=uuid4(), evaluation_state="evaluated")

    query, _ = conn.fetchval_calls[0]
    assert "ON CONFLICT (prediction_id)" in query


@pytest.mark.asyncio
async def test_evaluate_prediction_marks_not_evaluable(monkeypatch) -> None:
    candidate = normalized_candidate(build_candidate())
    writes: list[dict] = []

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return uuid4()

    monkeypatch.setattr("app.evaluation.service.write_evaluation_result", fake_write)

    state = await evaluate_prediction(candidate, evaluated_at=candidate.created_at)

    assert state == "not_evaluable"
    assert writes[0]["evaluation_state"] == "not_evaluable"


@pytest.mark.asyncio
async def test_evaluate_prediction_marks_void_when_prices_missing(monkeypatch) -> None:
    candidate = normalized_candidate(build_candidate())
    writes: list[dict] = []

    async def fake_get_price(*args, **kwargs):
        return None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return uuid4()

    monkeypatch.setattr("app.evaluation.service.get_price_at_or_before", fake_get_price)
    monkeypatch.setattr("app.evaluation.service.write_evaluation_result", fake_write)

    state = await evaluate_prediction(candidate, evaluated_at=candidate.horizon_end_at + timedelta(minutes=1))

    assert state == "void"
    assert writes[0]["evaluation_state"] == "void"


@pytest.mark.asyncio
async def test_evaluate_prediction_writes_evaluated_metrics(monkeypatch) -> None:
    candidate = normalized_candidate(build_candidate())
    writes: list[dict] = []
    prices = [100.0, 103.0]

    async def fake_get_price(*args, **kwargs):
        return prices.pop(0)

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return uuid4()

    monkeypatch.setattr("app.evaluation.service.get_price_at_or_before", fake_get_price)
    monkeypatch.setattr("app.evaluation.service.write_evaluation_result", fake_write)

    state = await evaluate_prediction(candidate, evaluated_at=candidate.horizon_end_at + timedelta(minutes=1))

    assert state == "evaluated"
    assert writes[0]["evaluation_state"] == "evaluated"
    assert writes[0]["actual_outcome"] == "hit"
    assert writes[0]["directional_correct"] is True
