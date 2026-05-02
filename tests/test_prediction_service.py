from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.predictions.contracts import DirectionRule, SettlementRule
from app.predictions.service import (
    PredictionCandidate,
    generate_prediction_for_candidate,
    get_or_create_model_version,
    read_active_targets,
    write_prediction_record,
    write_prediction_status,
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


def build_snapshot():
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
    return build_price_feature_snapshot(
        asset_id=asset_id,
        asset_symbol="BTC/USD",
        as_of_at=datetime(2026, 4, 18, 6, 0, tzinfo=UTC),
        price_bars=bars,
    )


def build_target():
    from app.predictions.contracts import PredictionTarget

    return PredictionTarget(
        id=uuid4(),
        name="btc_up_2pct_24h",
        asset_type="crypto",
        target_metric="price_return",
        horizon_hours=24,
        direction_rule=DirectionRule(direction="up", metric="price_return", threshold=0.02, unit="fraction"),
        settlement_rule=SettlementRule(type="continuous", horizon="wall_clock_hours", n=24, calendar="none"),
    )


def build_prediction_input(*, probability: float = 0.7, created_at: datetime | None = None):
    from app.predictions.contracts import PredictionInput

    snapshot = build_snapshot()
    return PredictionInput(
        target=build_target(),
        asset_id=snapshot.asset_id,
        asset_symbol=snapshot.asset_symbol,
        asset_type="crypto",
        feature_snapshot=snapshot,
        model_version_id=uuid4(),
        prompt_version_id=uuid4(),
        probability=probability,
        evidence_summary="Mean reversion setup with mild positive sentiment.",
        predicted_outcome="up_2pct",
        prediction_mode="live",
        created_at=created_at or datetime(2026, 4, 18, 6, 1, tzinfo=UTC),
        correlation_id=uuid4(),
        rationale={"model_name": "llm-prediction-engine"},
    )


@pytest.mark.asyncio
async def test_read_active_targets_parses_rules(monkeypatch) -> None:
    conn = FakeConnection(
        rows=[
            {
                "id": uuid4(),
                "name": "btc_up_2pct_24h",
                "asset_type": "crypto",
                "target_metric": "price_return",
                "horizon_hours": 24,
                "direction_rule": json.dumps({"direction": "up", "metric": "price_return", "threshold": 0.02, "unit": "fraction"}),
                "settlement_rule": json.dumps({"type": "continuous", "horizon": "wall_clock_hours", "n": 24, "calendar": "none"}),
                "asset_id": None,
                "is_active": True,
            }
        ]
    )
    monkeypatch.setattr("app.predictions.service.get_pool", lambda: FakePool(conn))

    targets = await read_active_targets()

    assert len(targets) == 1
    assert targets[0].direction_rule.direction == "up"


@pytest.mark.asyncio
async def test_get_or_create_model_version_inserts_when_missing(monkeypatch) -> None:
    conn = FakeConnection(fetchval_results=[None, uuid4()])
    monkeypatch.setattr("app.predictions.service.get_pool", lambda: FakePool(conn))

    model_id = await get_or_create_model_version()

    assert model_id is not None
    assert len(conn.fetchval_calls) == 2


@pytest.mark.asyncio
async def test_write_prediction_record_inserts_prediction(monkeypatch) -> None:
    conn = FakeConnection(fetchval_results=[uuid4()])
    monkeypatch.setattr("app.predictions.service.get_pool", lambda: FakePool(conn))
    from app.predictions.logic import build_prediction_record
    from app.predictions.heuristic import generate_heuristic_prediction_input
    from app.predictions.contracts import PredictionTarget

    snapshot = build_snapshot()
    target = PredictionTarget(
        id=uuid4(),
        name="btc_up_2pct_24h",
        asset_type="crypto",
        target_metric="price_return",
        horizon_hours=24,
        direction_rule=DirectionRule(direction="up", metric="price_return", threshold=0.02, unit="fraction"),
        settlement_rule=SettlementRule(type="continuous", horizon="wall_clock_hours", n=24, calendar="none"),
    )
    record = build_prediction_record(
        generate_heuristic_prediction_input(
            target=target,
            snapshot=snapshot,
            asset_type="crypto",
            model_version_id=uuid4(),
            created_at=datetime(2026, 4, 18, 6, 1, tzinfo=UTC),
            correlation_id=uuid4(),
        )
    )

    prediction_id = await write_prediction_record(record)

    assert prediction_id is not None
    insert_query, insert_args = conn.fetchval_calls[0]
    assert "llm_probability" in insert_query
    assert "pre_cal_probability" in insert_query
    assert len(insert_args) == 20


@pytest.mark.asyncio
async def test_write_prediction_status_inserts_history(monkeypatch) -> None:
    conn = FakeConnection(fetchval_results=[uuid4()])
    monkeypatch.setattr("app.predictions.service.get_pool", lambda: FakePool(conn))

    status_id = await write_prediction_status(uuid4(), "created", "test")

    assert status_id is not None


@pytest.mark.asyncio
async def test_generate_prediction_for_candidate_skips_duplicates(monkeypatch) -> None:
    snapshot = build_snapshot()
    from app.predictions.contracts import PredictionTarget

    candidate = PredictionCandidate(
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

    async def fake_prediction_exists(**kwargs):
        return True

    monkeypatch.setattr("app.predictions.service.prediction_exists", fake_prediction_exists)

    record = await generate_prediction_for_candidate(candidate, correlation_id=uuid4())

    assert record is None


@pytest.mark.asyncio
async def test_generate_prediction_for_candidate_creates_prediction(monkeypatch) -> None:
    snapshot = build_snapshot()
    from app.predictions.contracts import PredictionTarget

    candidate = PredictionCandidate(
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
    writes: list[str] = []

    async def fake_prediction_exists(**kwargs):
        return False

    async def fake_get_model_version():
        return uuid4()

    async def fake_write_prediction(record):
        writes.append("prediction")
        return record.id

    async def fake_write_status(*args, **kwargs):
        writes.append("status")
        return uuid4()

    monkeypatch.setattr("app.predictions.service.prediction_exists", fake_prediction_exists)
    monkeypatch.setattr("app.predictions.service.get_or_create_model_version", fake_get_model_version)
    monkeypatch.setattr("app.predictions.service.write_prediction_record", fake_write_prediction)
    monkeypatch.setattr("app.predictions.service.write_prediction_status", fake_write_status)

    record = await generate_prediction_for_candidate(candidate, correlation_id=uuid4())

    assert record is not None
    assert writes == ["prediction", "status"]


@pytest.mark.asyncio
async def test_generate_prediction_captures_llm_probability_before_blending(monkeypatch) -> None:
    prediction_input = build_prediction_input(probability=0.73)
    candidate = PredictionCandidate(
        target=prediction_input.target,
        snapshot=prediction_input.feature_snapshot,
        asset_type=prediction_input.asset_type,
    )
    written_records = []

    async def fake_prediction_exists(**kwargs):
        return False

    async def fake_generate_llm_prediction_input(**kwargs):
        return prediction_input

    async def fake_get_llm_model_version(*args):
        return uuid4()

    async def fake_blend(input_prediction):
        return input_prediction.model_copy(update={"probability": 0.61})

    async def fake_calibrate(input_prediction):
        return input_prediction

    async def fake_write_prediction(record):
        written_records.append(record)
        return record.id

    async def fake_write_status(*args, **kwargs):
        return uuid4()

    monkeypatch.setattr("app.predictions.service.prediction_exists", fake_prediction_exists)
    monkeypatch.setattr("app.predictions.service.get_model_client", lambda: object())
    monkeypatch.setattr("app.predictions.service.get_or_create_llm_model_version", fake_get_llm_model_version)
    monkeypatch.setattr("app.predictions.service.generate_llm_prediction_input", fake_generate_llm_prediction_input)
    monkeypatch.setattr("app.predictions.service.maybe_blend_with_ensemble", fake_blend)
    monkeypatch.setattr("app.predictions.service.maybe_apply_calibration", fake_calibrate)
    monkeypatch.setattr("app.predictions.service.write_prediction_record", fake_write_prediction)
    monkeypatch.setattr("app.predictions.service.write_prediction_status", fake_write_status)

    record = await generate_prediction_for_candidate(candidate, correlation_id=uuid4())

    assert record is written_records[0]
    assert record.llm_probability == 0.73
    assert record.probability == Decimal("0.61000")


@pytest.mark.asyncio
async def test_generate_prediction_captures_pre_cal_probability_before_calibration(monkeypatch) -> None:
    prediction_input = build_prediction_input(probability=0.73)
    candidate = PredictionCandidate(
        target=prediction_input.target,
        snapshot=prediction_input.feature_snapshot,
        asset_type=prediction_input.asset_type,
    )

    async def fake_prediction_exists(**kwargs):
        return False

    async def fake_generate_llm_prediction_input(**kwargs):
        return prediction_input

    async def fake_get_llm_model_version(*args):
        return uuid4()

    async def fake_blend(input_prediction):
        return input_prediction.model_copy(update={"probability": 0.62})

    async def fake_calibrate(input_prediction):
        return input_prediction.model_copy(
            update={
                "probability": 0.55,
                "rationale": {**input_prediction.rationale, "calibration_applied": True},
            }
        )

    async def fake_write_prediction(record):
        return record.id

    async def fake_write_status(*args, **kwargs):
        return uuid4()

    monkeypatch.setattr("app.predictions.service.prediction_exists", fake_prediction_exists)
    monkeypatch.setattr("app.predictions.service.get_model_client", lambda: object())
    monkeypatch.setattr("app.predictions.service.get_or_create_llm_model_version", fake_get_llm_model_version)
    monkeypatch.setattr("app.predictions.service.generate_llm_prediction_input", fake_generate_llm_prediction_input)
    monkeypatch.setattr("app.predictions.service.maybe_blend_with_ensemble", fake_blend)
    monkeypatch.setattr("app.predictions.service.maybe_apply_calibration", fake_calibrate)
    monkeypatch.setattr("app.predictions.service.write_prediction_record", fake_write_prediction)
    monkeypatch.setattr("app.predictions.service.write_prediction_status", fake_write_status)

    record = await generate_prediction_for_candidate(candidate, correlation_id=uuid4())

    assert record is not None
    assert record.llm_probability == 0.73
    assert record.pre_cal_probability == 0.62
    assert record.probability == Decimal("0.55000")


@pytest.mark.asyncio
async def test_generate_prediction_leaves_probability_decomposition_null_for_heuristic(monkeypatch) -> None:
    snapshot = build_snapshot()
    candidate = PredictionCandidate(target=build_target(), snapshot=snapshot, asset_type="crypto")

    async def fake_prediction_exists(**kwargs):
        return False

    async def fake_get_model_version():
        return uuid4()

    async def fake_write_prediction(record):
        return record.id

    async def fake_write_status(*args, **kwargs):
        return uuid4()

    monkeypatch.setattr("app.predictions.service.prediction_exists", fake_prediction_exists)
    monkeypatch.setattr("app.predictions.service.get_model_client", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("app.predictions.service.get_or_create_model_version", fake_get_model_version)
    monkeypatch.setattr("app.predictions.service.write_prediction_record", fake_write_prediction)
    monkeypatch.setattr("app.predictions.service.write_prediction_status", fake_write_status)

    record = await generate_prediction_for_candidate(candidate, correlation_id=uuid4())

    assert record is not None
    assert record.llm_probability is None
    assert record.pre_cal_probability is None
