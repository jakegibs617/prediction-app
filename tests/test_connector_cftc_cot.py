"""Unit tests for the CFTC COT connector. HTTP is mocked offline."""
from __future__ import annotations

from typing import Any

import pytest

from app.connectors import cftc_cot


SAMPLE_DISAGGREGATED_BODY = [
    {
        "id": "260407088691F",
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": "2026-04-07T00:00:00.000",
        "contract_market_name": "GOLD",
        "cftc_contract_market_code": "088691",
        "commodity_name": "GOLD",
        "open_interest_all": "523000",
        "m_money_positions_long_all": "198500",
        "m_money_positions_short_all": "74500",
        "futonly_or_combined": "FutOnly",
    },
    {
        "id": "260331088691F",
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": "2026-03-31T00:00:00.000",
        "contract_market_name": "GOLD",
        "cftc_contract_market_code": "088691",
        "commodity_name": "GOLD",
        "open_interest_all": "0",
        "m_money_positions_long_all": "100",
        "m_money_positions_short_all": "60",
        "futonly_or_combined": "FutOnly",
    },
    {
        "report_date_as_yyyy_mm_dd": "2026-03-24T00:00:00.000",
        "m_money_positions_long_all": ".",
        "m_money_positions_short_all": "60",
    },
]


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
async def test_fetch_route_computes_net_positioning(monkeypatch) -> None:
    fake_client = _FakeAsyncClient(SAMPLE_DISAGGREGATED_BODY)

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)

    connector = cftc_cot.CftcCotConnector()
    rows = await connector._fetch_route(cftc_cot.TRACKED_ROUTES[0], limit=3)

    assert len(rows) == 2
    assert rows[0]["net_position"] == 124000.0
    assert rows[0]["net_pct_open_interest"] == pytest.approx(124000.0 / 523000.0)
    assert rows[1]["net_position"] == 40.0
    assert rows[1]["net_pct_open_interest"] is None
    assert fake_client.calls[0][0].endswith("/resource/72hh-3qpy.json")
    assert fake_client.calls[0][1]["cftc_contract_market_code"] == "088691"
    assert fake_client.calls[0][1]["$order"] == "report_date_as_yyyy_mm_dd DESC"


@pytest.mark.asyncio
async def test_fetch_route_raises_on_unexpected_payload(monkeypatch) -> None:
    fake_client = _FakeAsyncClient({"error": "not a list"})

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)

    connector = cftc_cot.CftcCotConnector()
    with pytest.raises(RuntimeError, match="unexpected payload"):
        await connector._fetch_route(cftc_cot.TRACKED_ROUTES[0])


def test_parse_report_date() -> None:
    assert cftc_cot._parse_report_date("2026-04-07T00:00:00.000").date().isoformat() == "2026-04-07"
    assert cftc_cot._parse_report_date("2026-04-07").date().isoformat() == "2026-04-07"


def test_tracked_routes_have_distinct_series_ids() -> None:
    series_ids = [r.series_id for r in cftc_cot.TRACKED_ROUTES]
    assert len(series_ids) == len(set(series_ids))


def test_payload_shape_for_normalization_macro_validator() -> None:
    from app.normalization.pipeline import _validate_raw_payload

    raw_payload = {
        "series_id": "CFTC_COT_MANAGED_MONEY_NET_GOLD",
        "series_name": "CFTC COT Managed Money Net Positioning - Gold",
        "subtype": "cot_managed_money_net_positioning",
        "frequency": "weekly",
        "observation_date": "2026-04-07",
        "value": 124000.0,
        "units": "contracts",
        "long_positions": 198500.0,
        "short_positions": 74500.0,
        "open_interest": 523000.0,
        "applies_to_asset_type": "commodity",
    }

    assert _validate_raw_payload(raw_payload, "macro") == []
