from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.alerts.service import AlertRule, process_prediction_alert, read_alert_rules, write_alert_delivery
from app.predictions.logic import PredictionRecord


class FakeConnection:
    def __init__(self, rows: list[dict] | None = None, fetchval_results: list[object] | None = None) -> None:
        self.rows = rows or []
        self.fetchval_results = fetchval_results or []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        if self.fetchval_results:
            return self.fetchval_results.pop(0)
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


def build_prediction_record(*, probability: str = "0.87") -> PredictionRecord:
    created_at = datetime(2026, 4, 18, 14, 1, tzinfo=UTC)
    return PredictionRecord(
        id=uuid4(),
        target_id=uuid4(),
        asset_id=uuid4(),
        feature_snapshot_id=uuid4(),
        model_version_id=uuid4(),
        prompt_version_id=None,
        prediction_mode="live",
        predicted_outcome="up_2pct",
        probability=Decimal(probability),
        evidence_summary="Price and sentiment support a short-term rebound.",
        rationale={},
        created_at=created_at,
        horizon_end_at=created_at + timedelta(hours=24),
        correlation_id=uuid4(),
        hallucination_risk=False,
        probability_extreme_flag=False,
        context_compressed=False,
        backtest_run_id=None,
    )


@pytest.mark.asyncio
async def test_read_alert_rules_returns_active_rules(monkeypatch) -> None:
    conn = FakeConnection(
        rows=[
            {
                "id": uuid4(),
                "name": "default",
                "min_probability": Decimal("0.85"),
                "max_horizon_hours": 72,
                "channel_type": "telegram",
                "destination": "chat-1",
                "is_active": True,
            }
        ]
    )
    monkeypatch.setattr("app.alerts.service.get_pool", lambda: FakePool(conn))

    rules = await read_alert_rules()

    assert len(rules) == 1
    assert rules[0].destination == "chat-1"


@pytest.mark.asyncio
async def test_write_alert_delivery_uses_upsert(monkeypatch) -> None:
    conn = FakeConnection(fetchval_results=[uuid4()])
    monkeypatch.setattr("app.alerts.service.get_pool", lambda: FakePool(conn))

    await write_alert_delivery(
        prediction_id=uuid4(),
        alert_rule_id=uuid4(),
        delivery_status="sent",
        provider_message_id="99",
    )

    query, _ = conn.fetchval_calls[0]
    assert "ON CONFLICT (prediction_id, alert_rule_id)" in query


@pytest.mark.asyncio
async def test_process_prediction_alert_sends_and_logs_delivery(monkeypatch) -> None:
    prediction = build_prediction_record()
    rule = AlertRule(
        id=uuid4(),
        name="default",
        min_probability=Decimal("0.85"),
        max_horizon_hours=72,
        channel_type="telegram",
        destination="chat-1",
        is_active=True,
    )
    sent_messages: list[tuple[str, str]] = []
    writes: list[dict] = []

    async def fake_check_already_alerted(*args, **kwargs) -> bool:
        return False

    async def fake_send_telegram_message(destination: str, message: str):
        sent_messages.append((destination, message))
        from app.alerts.telegram import TelegramDeliveryResult

        return TelegramDeliveryResult(success=True, status_code=200, provider_message_id="123", error=None)

    async def fake_write_alert_delivery(**kwargs):
        writes.append(kwargs)
        return uuid4()

    monkeypatch.setattr("app.alerts.service.check_already_alerted", fake_check_already_alerted)
    monkeypatch.setattr("app.alerts.service.send_telegram_message", fake_send_telegram_message)
    monkeypatch.setattr("app.alerts.service.write_alert_delivery", fake_write_alert_delivery)

    results = await process_prediction_alert(
        prediction,
        asset_symbol="BTC/USD",
        target_metric="price_return",
        claim_type="correlation",
        rules=[rule],
    )

    assert len(results) == 1
    assert sent_messages[0][0] == "chat-1"
    assert writes[0]["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_process_prediction_alert_skips_already_alerted(monkeypatch) -> None:
    prediction = build_prediction_record()
    rule = AlertRule(
        id=uuid4(),
        name="default",
        min_probability=Decimal("0.85"),
        max_horizon_hours=72,
        channel_type="telegram",
        destination="chat-1",
        is_active=True,
    )

    async def fake_check_already_alerted(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr("app.alerts.service.check_already_alerted", fake_check_already_alerted)

    results = await process_prediction_alert(
        prediction,
        asset_symbol="BTC/USD",
        target_metric="price_return",
        claim_type="correlation",
        rules=[rule],
    )

    assert results == []
