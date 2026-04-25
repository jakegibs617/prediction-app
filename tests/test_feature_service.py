from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.features.engine import PriceBar
from app.features.service import (
    FeatureCandidate,
    generate_features_for_asset,
    get_or_create_feature_set,
    read_feature_candidates,
    read_price_bars,
    write_feature_lineage,
    write_feature_snapshot,
    write_feature_values,
)


class FakeConnection:
    def __init__(self, rows: list[dict] | None = None, fetchrow_results: list[dict | None] | None = None, fetchval_results: list[object] | None = None) -> None:
        self.rows = rows or []
        self.fetchrow_results = fetchrow_results or []
        self.fetchval_results = fetchval_results or []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        return self.fetchval_results.pop(0) if self.fetchval_results else uuid4()

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return None

    async def executemany(self, query: str, args_list):
        self.executemany_calls.append((query, list(args_list)))
        return None


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
    from app.features.engine import build_price_feature_snapshot

    asset_id = uuid4()
    start = datetime(2026, 4, 17, 0, 0, tzinfo=UTC)
    bars = [
        PriceBar(
            asset_id=asset_id,
            source_record_id=uuid4(),
            bar_start_at=start + timedelta(hours=index),
            bar_end_at=start + timedelta(hours=index, minutes=59),
            close=100 + index,
        )
        for index in range(30)
    ]
    return build_price_feature_snapshot(
        asset_id=asset_id,
        asset_symbol="BTC/USD",
        as_of_at=datetime(2026, 4, 18, 5, 0, tzinfo=UTC),
        price_bars=bars,
        feature_set_name="price-baseline-v1",
    )


@pytest.mark.asyncio
async def test_get_or_create_feature_set_inserts_when_missing(monkeypatch) -> None:
    conn = FakeConnection(fetchval_results=[None, uuid4()])
    monkeypatch.setattr("app.features.service.get_pool", lambda: FakePool(conn))

    feature_set_id = await get_or_create_feature_set()

    assert feature_set_id is not None
    assert len(conn.fetchval_calls) == 2


@pytest.mark.asyncio
async def test_read_feature_candidates_reads_active_assets(monkeypatch) -> None:
    conn = FakeConnection(rows=[{"id": uuid4(), "symbol": "BTC/USD"}])
    monkeypatch.setattr("app.features.service.get_pool", lambda: FakePool(conn))

    candidates = await read_feature_candidates()

    assert len(candidates) == 1
    assert candidates[0].asset_symbol == "BTC/USD"


@pytest.mark.asyncio
async def test_read_price_bars_maps_rows(monkeypatch) -> None:
    asset_id = uuid4()
    source_id = uuid4()
    raw_source_record_id = uuid4()
    conn = FakeConnection(
        rows=[
            {
                "asset_id": asset_id,
                "source_id": source_id,
                "bar_interval": "30m",
                "bar_start_at": datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
                "bar_end_at": datetime(2026, 4, 18, 0, 29, tzinfo=UTC),
                "close": 101.5,
                "symbol": "BTC/USD",
            }
        ],
        fetchval_results=[raw_source_record_id],
    )
    monkeypatch.setattr("app.features.service.get_pool", lambda: FakePool(conn))

    bars = await read_price_bars(asset_id, datetime(2026, 4, 18, 5, 0, tzinfo=UTC))

    assert len(bars) == 1
    assert bars[0].close == 101.5
    assert bars[0].source_record_id == raw_source_record_id


@pytest.mark.asyncio
async def test_write_feature_snapshot_values_and_lineage(monkeypatch) -> None:
    snapshot = build_snapshot()
    conn = FakeConnection()
    monkeypatch.setattr("app.features.service.get_pool", lambda: FakePool(conn))

    await write_feature_snapshot(snapshot, feature_set_id=uuid4())
    await write_feature_values(snapshot)
    await write_feature_lineage(snapshot)

    assert len(conn.execute_calls) >= 1
    assert len(conn.executemany_calls) >= 1


@pytest.mark.asyncio
async def test_generate_features_for_asset_skips_existing_snapshot(monkeypatch) -> None:
    candidate = FeatureCandidate(asset_id=uuid4(), asset_symbol="BTC/USD")

    async def fake_get_feature_set():
        return uuid4()

    async def fake_exists(**kwargs):
        return True

    monkeypatch.setattr("app.features.service.get_or_create_feature_set", fake_get_feature_set)
    monkeypatch.setattr("app.features.service.feature_snapshot_exists", fake_exists)

    snapshot = await generate_features_for_asset(candidate, as_of_at=datetime(2026, 4, 18, 5, 0, tzinfo=UTC))

    assert snapshot is None


@pytest.mark.asyncio
async def test_generate_features_for_asset_creates_snapshot(monkeypatch) -> None:
    candidate = FeatureCandidate(asset_id=uuid4(), asset_symbol="BTC/USD")
    start = datetime(2026, 4, 17, 0, 0, tzinfo=UTC)
    bars = [
        PriceBar(
            asset_id=candidate.asset_id,
            source_record_id=uuid4(),
            bar_start_at=start + timedelta(hours=index),
            bar_end_at=start + timedelta(hours=index, minutes=59),
            close=100 + index,
        )
        for index in range(30)
    ]
    writes: list[str] = []

    async def fake_get_feature_set():
        return uuid4()

    async def fake_exists(**kwargs):
        return False

    async def fake_read_price_bars(asset_id, cutoff_time):
        return bars

    async def fake_write_snapshot(snapshot, *, feature_set_id):
        writes.append("snapshot")
        return snapshot.snapshot_id

    async def fake_write_values(snapshot):
        writes.append("values")

    async def fake_write_lineage(snapshot):
        writes.append("lineage")

    monkeypatch.setattr("app.features.service.get_or_create_feature_set", fake_get_feature_set)
    monkeypatch.setattr("app.features.service.feature_snapshot_exists", fake_exists)
    monkeypatch.setattr("app.features.service.read_price_bars", fake_read_price_bars)
    monkeypatch.setattr("app.features.service.write_feature_snapshot", fake_write_snapshot)
    monkeypatch.setattr("app.features.service.write_feature_values", fake_write_values)
    monkeypatch.setattr("app.features.service.write_feature_lineage", fake_write_lineage)

    snapshot = await generate_features_for_asset(candidate, as_of_at=datetime(2026, 4, 18, 5, 0, tzinfo=UTC))

    assert snapshot is not None
    assert writes == ["snapshot", "values", "lineage"]
