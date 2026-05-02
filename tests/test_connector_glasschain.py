"""Unit tests for the Glassnode on-chain connector. HTTP is mocked offline."""
from __future__ import annotations

from typing import Any

import pytest

from app.connectors import glasschain


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> object:
        return self._body


class _FakeAsyncClient:
    def __init__(self, body: object) -> None:
        self._body = body
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, headers: dict, params: dict) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse(200, self._body)


@pytest.mark.asyncio
async def test_fetch_metric_filters_to_numeric_rows(monkeypatch) -> None:
    body = [
        {"t": 1777075200, "v": -1245.5},
        {"t": 1777161600, "v": "88.2"},
        {"t": 1777248000, "v": {"unexpected": "object"}},
        {"t": "bad", "v": 10},
        {"t": 1777334400, "v": None},
    ]
    fake_client = _FakeAsyncClient(body)

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)
    monkeypatch.setattr(glasschain.settings, "glassnode_api_key", "DUMMY_KEY")

    connector = glasschain.GlasschainConnector()
    rows = await connector._fetch_metric(glasschain.TRACKED_METRICS[0], asset="BTC")

    assert rows == [
        {"timestamp": 1777075200, "value": -1245.5},
        {"timestamp": 1777161600, "value": 88.2},
    ]
    assert fake_client.calls[0][0].endswith("/v1/metrics/transactions/transfers_volume_exchanges_net")
    assert fake_client.calls[0][1]["a"] == "BTC"
    assert fake_client.calls[0][1]["c"] == "NATIVE"


@pytest.mark.asyncio
async def test_fetch_metric_raises_on_error_payload(monkeypatch) -> None:
    fake_client = _FakeAsyncClient({"error": "invalid api key"})

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)
    monkeypatch.setattr(glasschain.settings, "glassnode_api_key", "DUMMY_KEY")

    connector = glasschain.GlasschainConnector()
    with pytest.raises(RuntimeError, match="Glassnode error"):
        await connector._fetch_metric(glasschain.TRACKED_METRICS[0], asset="BTC")


def test_fetch_raises_on_missing_api_key(monkeypatch) -> None:
    connector = glasschain.GlasschainConnector()

    monkeypatch.setattr(glasschain.settings, "glassnode_api_key", "")
    with pytest.raises(RuntimeError, match="GLASSNODE_API_KEY"):
        connector._api_key()

    monkeypatch.setattr(glasschain.settings, "glassnode_api_key", "REPLACE_ME")
    with pytest.raises(RuntimeError, match="GLASSNODE_API_KEY"):
        connector._api_key()


def test_payload_shape_for_normalization_macro_validator() -> None:
    from app.normalization.pipeline import _validate_raw_payload

    metric = glasschain.TRACKED_METRICS[0]
    raw_payload = {
        "series_id": glasschain.build_series_id(metric, "BTC"),
        "series_name": metric.name,
        "subtype": metric.subtype,
        "asset": "BTC",
        "frequency": glasschain.DEFAULT_INTERVAL,
        "observation_date": "2026-04-25",
        "value": -1245.5,
        "units": metric.units,
        "applies_to_asset_type": "crypto",
    }

    assert _validate_raw_payload(raw_payload, "macro") == []


def test_tracked_series_ids_are_distinct_per_asset() -> None:
    series_ids = [
        glasschain.build_series_id(metric, asset)
        for metric in glasschain.TRACKED_METRICS
        for asset in metric.assets
    ]
    assert len(series_ids) == len(set(series_ids))
