"""Unit tests for the Crypto Fear & Greed connector.

We mock httpx so the test runs offline and is deterministic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.connectors import fear_greed as fg


class _FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict:
        return self._body


class _FakeAsyncClient:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.calls: list[tuple[str, dict]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        return super().__init_subclass__(**kwargs)

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, headers: dict, params: dict) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse(200, self._body)


@pytest.fixture
def sample_body() -> dict:
    return {
        "name": "Fear and Greed Index",
        "data": [
            {
                "value": "31",
                "value_classification": "Fear",
                "timestamp": "1777075200",
                "time_until_update": "44245",
            },
            {
                "value": "44",
                "value_classification": "Fear",
                "timestamp": "1776988800",
            },
            {
                "value": "bad",   # invalid -> should be filtered out
                "value_classification": "?",
                "timestamp": "1776902400",
            },
        ],
        "metadata": {"error": None},
    }


@pytest.mark.asyncio
async def test_fetch_returns_only_valid_rows(monkeypatch, sample_body: dict) -> None:
    fake_client = _FakeAsyncClient(sample_body)

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)

    connector = fg.FearGreedConnector()
    items = await connector._fetch(limit=5)

    assert len(items) == 2
    assert items[0]["value"] == 31
    assert items[0]["value_classification"] == "Fear"
    assert isinstance(items[0]["timestamp"], int)


@pytest.mark.asyncio
async def test_fetch_raises_on_metadata_error(monkeypatch) -> None:
    body = {"name": "x", "data": [], "metadata": {"error": "broken"}}
    fake_client = _FakeAsyncClient(body)

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)

    connector = fg.FearGreedConnector()
    with pytest.raises(RuntimeError, match="alternative.me error"):
        await connector._fetch(limit=5)


def test_payload_shape_for_normalization_macro_validator() -> None:
    """Sanity: build the same raw_payload shape the run() method does and
    verify it satisfies the normalization pipeline's macro validator."""
    from app.normalization.pipeline import _validate_raw_payload

    timestamp = int(datetime(2026, 4, 25, tzinfo=timezone.utc).timestamp())
    obs_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    raw_payload = {
        "series_id": fg.SERIES_ID,
        "series_name": fg.SERIES_NAME,
        "subtype": fg.SUBTYPE,
        "observation_date": obs_dt.strftime("%Y-%m-%d"),
        "value": 31,
        "value_classification": "Fear",
        "scale_min": 0,
        "scale_max": 100,
        "applies_to_asset_type": "crypto",
    }
    errors = _validate_raw_payload(raw_payload, "macro")
    assert errors == []


def test_payload_is_json_round_trippable() -> None:
    """Ensures every field in the payload is JSON-serializable, since
    the writer uses json.dumps before insertion to the jsonb column."""
    raw_payload = {
        "series_id": fg.SERIES_ID,
        "value": 31,
        "value_classification": "Fear",
        "scale_min": 0,
        "scale_max": 100,
    }
    encoded = json.dumps(raw_payload)
    assert json.loads(encoded) == raw_payload
