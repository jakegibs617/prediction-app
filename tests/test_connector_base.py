from __future__ import annotations

from uuid import uuid4

import pytest

from app.connectors.base import BaseConnector


class DummyConnector(BaseConnector):
    source_name = "dummy"
    category = "news"
    base_url = "https://example.com"

    async def run(self) -> None:
        return None


class FakeConnection:
    def __init__(self, latest_row=None) -> None:
        self.latest_row = latest_row
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return self.latest_row


@pytest.mark.asyncio
async def test_plan_raw_record_write_returns_first_version_for_new_record() -> None:
    connector = DummyConnector()
    conn = FakeConnection(latest_row=None)

    plan = await connector.plan_raw_record_write(
        conn,
        source_id=uuid4(),
        external_id="source-1",
        raw_payload={"headline": "hello"},
    )

    assert plan.should_write
    assert plan.record_version == 1
    assert plan.prior_record_id is None


@pytest.mark.asyncio
async def test_plan_raw_record_write_skips_identical_payload() -> None:
    connector = DummyConnector()
    source_id = uuid4()
    latest_id = uuid4()
    payload = {"headline": "same"}
    conn = FakeConnection(
        latest_row={
            "id": latest_id,
            "record_version": 3,
            "checksum": connector.plan_raw_record_write.__globals__["compute_checksum"](payload),
        }
    )

    plan = await connector.plan_raw_record_write(
        conn,
        source_id=source_id,
        external_id="source-1",
        raw_payload=payload,
    )

    assert not plan.should_write
    assert plan.record_version == 3
    assert plan.prior_record_id == latest_id


@pytest.mark.asyncio
async def test_plan_raw_record_write_versions_revised_payload() -> None:
    connector = DummyConnector()
    latest_id = uuid4()
    conn = FakeConnection(
        latest_row={
            "id": latest_id,
            "record_version": 2,
            "checksum": "old-checksum",
        }
    )

    plan = await connector.plan_raw_record_write(
        conn,
        source_id=uuid4(),
        external_id="source-1",
        raw_payload={"headline": "revised"},
    )

    assert plan.should_write
    assert plan.record_version == 3
    assert plan.prior_record_id == latest_id
