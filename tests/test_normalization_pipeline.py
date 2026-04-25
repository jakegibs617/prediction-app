from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.normalization.pipeline import NormalizationPipeline, _validate_raw_payload


class FakeConnection:
    def __init__(self, rows_batches: list[list[dict]]) -> None:
        self.rows_batches = rows_batches
        self.fetch_queries: list[str] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.statuses: dict[object, str] = {}

    async def fetch(self, query: str, *args):
        self.fetch_queries.append(query)
        return self.rows_batches.pop(0) if self.rows_batches else []

    async def execute(self, query: str, *args) -> None:
        self.execute_calls.append((query, args))
        if "SET validation_status = 'valid'" in query:
            self.statuses[args[-1]] = "valid"
        elif "SET validation_status = 'quarantined'" in query:
            self.statuses[args[-1]] = "quarantined"
        elif "SET validation_status = 'rejected'" in query:
            self.statuses[args[-1]] = "rejected"

    async def fetchval(self, query: str, *args):
        if "SELECT validation_status" in query:
            return self.statuses.get(args[0])
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


def _patch_pipeline_dependencies(monkeypatch, conn: FakeConnection) -> None:
    async def _acquire_job_lock(*args, **kwargs):
        return uuid4()

    async def _increment_attempt(*args, **kwargs):
        return 1

    async def _release_job_lock(*args, **kwargs):
        return None

    async def _send_to_dead_letter(*args, **kwargs):
        return None

    monkeypatch.setattr("app.normalization.pipeline.get_pool", lambda: FakePool(conn))
    monkeypatch.setattr("app.normalization.pipeline.get_cheap_model_client", lambda: object())
    monkeypatch.setattr("app.normalization.pipeline.acquire_job_lock", _acquire_job_lock)
    monkeypatch.setattr("app.normalization.pipeline.increment_attempt", _increment_attempt)
    monkeypatch.setattr("app.normalization.pipeline.release_job_lock", _release_job_lock)
    monkeypatch.setattr("app.normalization.pipeline.send_to_dead_letter", _send_to_dead_letter)


def test_validate_raw_payload_rejects_empty_structured_payload() -> None:
    assert _validate_raw_payload({}, "market_data") == ["raw_payload must not be empty"]


@pytest.mark.asyncio
async def test_normalization_pipeline_rejects_unknown_categories(monkeypatch) -> None:
    record_id = uuid4()
    conn = FakeConnection(
        rows_batches=[
            [
                {
                    "id": record_id,
                    "raw_payload": {"headline": "something happened"},
                    "source_id": uuid4(),
                    "source_recorded_at": datetime(2026, 4, 18, tzinfo=UTC),
                    "category": "mystery",
                }
            ],
            [],
        ]
    )
    _patch_pipeline_dependencies(monkeypatch, conn)

    await NormalizationPipeline().run()

    assert conn.statuses[record_id] == "rejected"


@pytest.mark.asyncio
async def test_normalization_pipeline_fetches_only_pending_records(monkeypatch) -> None:
    conn = FakeConnection(rows_batches=[[]])
    _patch_pipeline_dependencies(monkeypatch, conn)

    await NormalizationPipeline().run()

    assert any("WHERE rsr.validation_status = 'pending'" in query for query in conn.fetch_queries)
