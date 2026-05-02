"""Unit tests for the Cboe options sentiment connector. HTTP is mocked offline."""
from __future__ import annotations

from typing import Any

import pytest

from app.connectors import cboe_options


SAMPLE_OPTIONS_HTML = """
<h1>Cboe Exchange Market Statistics for Friday, May 1, 2026</h1>
<h3>Total</h3>
TIME CALLS PUTS TOTAL P/C RATIO
09:00 AM 1,157,212 813,436 1,970,648 0.70
03:15 PM 7,280,944 5,533,989 12,814,933 0.76
<h3>Index Options</h3>
TIME CALLS PUTS TOTAL P/C RATIO
09:00 AM 425,054 383,921 808,975 0.90
03:15 PM 2,862,030 2,811,062 5,673,092 0.98
<h3>Equity Options</h3>
TIME CALLS PUTS TOTAL P/C RATIO
09:00 AM 732,158 429,515 1,161,673 0.59
03:15 PM 4,418,914 2,722,927 7,141,841 0.62
"""


SAMPLE_VIX_HISTORY_CSV = """DATE,OPEN,HIGH,LOW,CLOSE
04/30/2026,18.680000,18.730000,16.870000,16.890000
05/01/2026,17.010000,17.390000,16.440000,16.990000
"""


SAMPLE_FUTURES_HTML = """
<h3>Settlement Prices for 2026-05-01</h3>
<h4>VX - Cboe Volatility Index (VX) Futures</h4>
Symbol - Expiration Date Settlement Price
VX18/K6 - 2026-05-06 19.6928
VX19/K6 - 2026-05-13 20.125*
VX/K6 - 2026-05-19 20.7897
<h4>VXM - Cboe Volatility Index Mini (VXM) Futures</h4>
"""


class _FakeResponse:
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.text = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> object:
        return {}


class _FakeAsyncClient:
    def __init__(self, bodies: list[str]) -> None:
        self._bodies = bodies
        self.calls: list[str] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, headers: dict, params: dict) -> _FakeResponse:
        self.calls.append(url)
        return _FakeResponse(200, self._bodies.pop(0))


def test_parse_put_call_observations_uses_latest_intraday_row() -> None:
    rows = cboe_options.parse_put_call_observations(SAMPLE_OPTIONS_HTML)

    assert [row.series_id for row in rows] == [
        "CBOE_PUT_CALL_TOTAL",
        "CBOE_PUT_CALL_INDEX",
        "CBOE_PUT_CALL_EQUITY",
    ]
    assert rows[0].observation_date == "2026-05-01"
    assert rows[0].value == 0.76
    assert rows[0].raw_details["time_label"] == "03:15 PM"
    assert rows[2].value == 0.62


def test_parse_vix_history_latest() -> None:
    row = cboe_options.parse_vix_history_latest(SAMPLE_VIX_HISTORY_CSV)

    assert row.series_id == "CBOE_VIX_SPOT_CLOSE"
    assert row.observation_date == "2026-05-01"
    assert row.value == 16.99


def test_parse_vix_futures_settlements() -> None:
    observation_date, rows = cboe_options.parse_vix_futures_settlements(SAMPLE_FUTURES_HTML)

    assert observation_date == "2026-05-01"
    assert len(rows) == 3
    assert rows[0].symbol == "VX18/K6"
    assert rows[0].settlement_price == 19.6928
    assert rows[1].discretionary_settlement is True


def test_build_vix_term_structure_observations() -> None:
    spot = cboe_options.parse_vix_history_latest(SAMPLE_VIX_HISTORY_CSV)
    observation_date, settlements = cboe_options.parse_vix_futures_settlements(SAMPLE_FUTURES_HTML)

    rows = cboe_options.build_vix_term_structure_observations(
        observation_date=observation_date,
        spot=spot,
        settlements=settlements,
    )

    by_series = {row.series_id: row for row in rows}
    assert by_series["CBOE_VIX_FRONT_FUTURE_SETTLE"].value == 19.6928
    assert by_series["CBOE_VIX_SECOND_FUTURE_SETTLE"].value == 20.125
    assert by_series["CBOE_VIX_FRONT_PREMIUM_TO_SPOT"].value == pytest.approx(2.7028)
    assert by_series["CBOE_VIX_1M_2M_FUTURES_SLOPE"].value == pytest.approx(0.4322)


@pytest.mark.asyncio
async def test_fetch_methods_call_public_cboe_urls(monkeypatch) -> None:
    fake_client = _FakeAsyncClient([SAMPLE_OPTIONS_HTML, SAMPLE_VIX_HISTORY_CSV, SAMPLE_FUTURES_HTML])

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr("app.connectors.base.httpx.AsyncClient", _factory)

    connector = cboe_options.CboeOptionsConnector()
    put_call = await connector._fetch_put_call_observations()
    spot = await connector._fetch_vix_spot()
    term = await connector._fetch_vix_term_structure(spot)

    assert len(put_call) == 3
    assert spot.value == 16.99
    assert len(term) == 4
    assert fake_client.calls == [
        cboe_options.OPTIONS_MARKET_STATS_URL,
        cboe_options.VIX_HISTORY_URL,
        cboe_options.FUTURES_SETTLEMENT_URL,
    ]


def test_payload_shape_for_normalization_macro_validator() -> None:
    from app.normalization.pipeline import _validate_raw_payload

    raw_payload = {
        "series_id": "CBOE_PUT_CALL_TOTAL",
        "series_name": "Cboe Total Options Put/Call Ratio",
        "subtype": "options_put_call_total",
        "frequency": "daily",
        "observation_date": "2026-05-01",
        "value": 0.76,
        "units": "ratio",
        "applies_to_asset_type": "all",
    }

    assert _validate_raw_payload(raw_payload, "macro") == []
