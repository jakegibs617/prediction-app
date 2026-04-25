from __future__ import annotations

import httpx
import pytest

from app.alerts.telegram import format_telegram_message, send_telegram_message


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.telegram.org")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    def json(self) -> dict:
        return self._json_data


class FakeClient:
    def __init__(self, responses: list[FakeResponse | Exception], calls: list[dict]) -> None:
        self.responses = responses
        self.calls = calls

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, json: dict) -> FakeResponse:
        self.calls.append({"url": url, "json": json})
        next_item = self.responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


def test_format_telegram_message_includes_claim_warning() -> None:
    message = format_telegram_message(
        {
            "prediction_id": "pred-1",
            "asset_symbol": "BTC/USD",
            "target_metric": "price_return",
            "predicted_outcome": "up_2pct",
            "probability": "0.87",
            "forecast_horizon_hours": "24",
            "evidence_summary": "Mean reversion setup.",
            "claim_type_warning": "Correlation only - causation not established",
        }
    )
    assert "BTC/USD" in message
    assert "Correlation only" in message


@pytest.mark.asyncio
async def test_send_telegram_message_retries_and_returns_message_id(monkeypatch) -> None:
    calls: list[dict] = []
    responses = [
        FakeResponse(500),
        FakeResponse(200, {"result": {"message_id": 42}}),
    ]
    monkeypatch.setattr("app.alerts.telegram.settings.telegram_bot_token", "secret-token")
    monkeypatch.setattr("app.alerts.telegram.httpx.AsyncClient", lambda timeout: FakeClient(responses, calls))

    result = await send_telegram_message("chat-1", "hello", max_attempts=2, base_delay=0)

    assert result.success
    assert result.provider_message_id == "42"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_send_telegram_message_fails_without_token(monkeypatch) -> None:
    monkeypatch.setattr("app.alerts.telegram.settings.telegram_bot_token", "")

    result = await send_telegram_message("chat-1", "hello")

    assert not result.success
    assert "not configured" in (result.error or "")
