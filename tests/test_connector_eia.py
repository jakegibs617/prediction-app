"""Unit tests for the EIA v2 connector. HTTP is mocked so tests run offline."""
from __future__ import annotations

from typing import Any

import pytest

from app.connectors import eia


WTI_SAMPLE_BODY = {
    "warnings": [],
    "response": {
        "total": "10147",
        "dateFormat": "YYYY-MM-DD",
        "frequency": "daily",
        "data": [
            {
                "period": "2026-04-20",
                "duoarea": "YCUOK",
                "area-name": "NA",
                "product": "EPCWTI",
                "product-name": "WTI Crude Oil",
                "process": "PF4",
                "process-name": "Spot Price FOB",
                "series": "RWTC",
                "series-description": "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)",
                "value": "91.06",
                "units": "$/BBL",
            },
            {
                "period": "2026-04-17",
                "series": "RWTC",
                "series-description": "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)",
                "value": "85.91",
                "units": "$/BBL",
            },
            {
                # Should be skipped: "." is EIA's not-yet-released marker
                "period": "2026-04-16",
                "series": "RWTC",
                "value": ".",
                "units": "$/BBL",
            },
            {
                # Should be skipped: missing value
                "period": "2026-04-15",
                "series": "RWTC",
                "value": None,
                "units": "$/BBL",
            },
        ],
    },
}


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

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, headers: dict, params: dict) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse(200, self._body)


@pytest.mark.asyncio
async def test_fetch_filters_invalid_values(monkeypatch) -> None:
    fake_client = _FakeAsyncClient(WTI_SAMPLE_BODY)

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)
    monkeypatch.setattr(eia.settings, "eia_api_key", "DUMMY_KEY")

    connector = eia.EiaConnector()
    rows = await connector._fetch_route(eia.TRACKED_ROUTES[0], length=10)

    assert len(rows) == 2
    assert rows[0]["value"] == 91.06
    assert rows[0]["units"] == "$/BBL"
    assert rows[1]["value"] == 85.91


@pytest.mark.asyncio
async def test_fetch_raises_on_missing_api_key(monkeypatch) -> None:
    monkeypatch.setattr(eia.settings, "eia_api_key", "")
    connector = eia.EiaConnector()
    with pytest.raises(RuntimeError, match="EIA_API_KEY"):
        connector._api_key()

    monkeypatch.setattr(eia.settings, "eia_api_key", "REPLACE_ME")
    with pytest.raises(RuntimeError, match="EIA_API_KEY"):
        connector._api_key()


def test_parse_period_daily_and_monthly() -> None:
    assert eia.EiaConnector._parse_period("2026-04-20", "daily").year == 2026
    assert eia.EiaConnector._parse_period("2026-04-20", "weekly").day == 20
    assert eia.EiaConnector._parse_period("2026-04", "monthly").month == 4


def test_routes_have_distinct_series_ids() -> None:
    series_ids = [r.series_id for r in eia.TRACKED_ROUTES]
    assert len(series_ids) == len(set(series_ids)), "duplicate series_id in TRACKED_ROUTES"


def test_payload_shape_for_normalization_macro_validator() -> None:
    """Sanity: EIA payloads pass the normalization macro validator."""
    from app.normalization.pipeline import _validate_raw_payload

    raw_payload = {
        "series_id": "RWTC",
        "series_name": "WTI Crude Oil Spot Price",
        "subtype": "commodity_price_oil",
        "frequency": "daily",
        "observation_date": "2026-04-20",
        "value": 91.06,
        "units": "$/BBL",
        "series_description": "Cushing, OK WTI Spot Price FOB",
    }
    assert _validate_raw_payload(raw_payload, "macro") == []
