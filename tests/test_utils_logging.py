from __future__ import annotations

from uuid import uuid4

import pytest

from app.utils import logging as logging_utils


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args) -> None:
        self.calls.append((query, args))


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
async def test_log_audit_uses_schema_column_names(monkeypatch) -> None:
    conn = FakeConnection()
    monkeypatch.setattr(logging_utils, "get_pool", lambda: FakePool(conn))

    correlation_id = uuid4()
    await logging_utils.log_audit(
        "prediction",
        uuid4(),
        "created",
        actor_type="system",
        details={"foo": "bar"},
        correlation_id=correlation_id,
    )

    query, args = conn.calls[0]
    assert "correlation_id" in query
    assert "details" in query
    assert args[3] == correlation_id
    assert args[5] == {"foo": "bar"}
