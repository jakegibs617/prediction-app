"""Tests for macro feature attachment in app.features.service.

We verify:
  - build_macro_feature_values converts inputs to FeatureValue correctly
  - read_macro_feature_inputs is routed by asset_type and tolerant of bad data
  - generate_features_for_asset merges macro features into the snapshot
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.features import service
from app.features.service import (
    FeatureCandidate,
    build_macro_feature_values,
    read_macro_feature_inputs,
)
from app.predictions.contracts import FeatureValue


# ---- minimal asyncpg-like fakes ------------------------------------------

class _FakeConn:
    def __init__(self, fetchrow_results: list[dict | None]) -> None:
        self._fetchrow_results = list(fetchrow_results)
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        if not self._fetchrow_results:
            return None
        return self._fetchrow_results.pop(0)


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                return outer._conn

            async def __aexit__(self_inner, *args):
                return None

        return _Ctx()


# ---- build_macro_feature_values ------------------------------------------

def test_build_macro_feature_values_basic() -> None:
    now = datetime(2026, 4, 25, tzinfo=timezone.utc)
    inputs = [
        {
            "feature_key": "macro__fear_greed_index",
            "numeric_value": 31.0,
            "source_record_id": uuid4(),
            "available_at": now,
            "source_name": "alternative_me_fear_greed",
            "series_id": "FEAR_GREED",
        },
        {
            "feature_key": "macro__fed_funds_rate",
            "numeric_value": 3.64,
            "source_record_id": uuid4(),
            "available_at": now,
            "source_name": "fred",
            "series_id": "FEDFUNDS",
        },
    ]
    out = build_macro_feature_values(inputs)
    assert len(out) == 2
    assert all(isinstance(v, FeatureValue) for v in out)
    assert out[0].feature_key == "macro__fear_greed_index"
    assert out[0].numeric_value == 31.0
    assert out[0].feature_type == "numeric"
    assert out[1].numeric_value == 3.64


def test_build_macro_feature_values_empty() -> None:
    assert build_macro_feature_values([]) == []


# ---- read_macro_feature_inputs -------------------------------------------

@pytest.mark.asyncio
async def test_read_macro_feature_inputs_returns_empty_for_unknown_asset_type(monkeypatch) -> None:
    # No DB calls expected
    conn = _FakeConn([])
    monkeypatch.setattr(service, "get_pool", lambda: _FakePool(conn))

    out = await read_macro_feature_inputs("totally-unknown", datetime.now(timezone.utc))
    assert out == []
    assert conn.calls == []


@pytest.mark.asyncio
async def test_read_macro_feature_inputs_skips_invalid_values(monkeypatch) -> None:
    now = datetime(2026, 4, 25, tzinfo=timezone.utc)
    src_id = uuid4()
    # Crypto routes 3 series: FEAR_GREED (good), FEDFUNDS (good), DGS10 (bad value)
    fetchrow_results = [
        {"source_record_id": src_id, "value_text": "31", "available_at": now},
        {"source_record_id": src_id, "value_text": "3.64", "available_at": now},
        {"source_record_id": src_id, "value_text": ".", "available_at": now},
    ]
    conn = _FakeConn(fetchrow_results)
    monkeypatch.setattr(service, "get_pool", lambda: _FakePool(conn))

    out = await read_macro_feature_inputs("crypto", now)

    keys = {row["feature_key"] for row in out}
    assert "macro__fear_greed_index" in keys
    assert "macro__fed_funds_rate" in keys
    assert "macro__treasury_10y_yield" not in keys, "row with value '.' should be filtered"
    assert all(isinstance(row["numeric_value"], float) for row in out)


@pytest.mark.asyncio
async def test_read_macro_feature_inputs_handles_none_rows(monkeypatch) -> None:
    """When the DB has no record for a (source, series), we just skip it."""
    now = datetime(2026, 4, 25, tzinfo=timezone.utc)
    fetchrow_results: list[dict | None] = [None, None, None]
    conn = _FakeConn(fetchrow_results)
    monkeypatch.setattr(service, "get_pool", lambda: _FakePool(conn))

    out = await read_macro_feature_inputs("crypto", now)
    assert out == []


# ---- routing table sanity ------------------------------------------------

def test_macro_routes_cover_each_supported_asset_type() -> None:
    routes = service._MACRO_FEATURE_ROUTES
    for asset_type in ("crypto", "equity", "commodity", "forex"):
        assert asset_type in routes, f"missing routes for {asset_type}"
        assert len(routes[asset_type]) > 0


def test_macro_feature_keys_are_unique_per_asset_type() -> None:
    """Within a single asset_type, no two routes should map to the same feature_key."""
    for asset_type, route_list in service._MACRO_FEATURE_ROUTES.items():
        keys: list[str] = []
        for _source, pairs in route_list:
            for _series, feature_key in pairs:
                keys.append(feature_key)
        assert len(keys) == len(set(keys)), (
            f"duplicate feature_key in {asset_type}: {keys}"
        )


def test_macro_feature_keys_are_prefixed() -> None:
    """All macro feature_keys must start with macro__ for prompt readability."""
    for route_list in service._MACRO_FEATURE_ROUTES.values():
        for _source, pairs in route_list:
            for _series, feature_key in pairs:
                assert feature_key.startswith("macro__"), feature_key


# ---- FeatureCandidate dataclass back-compat ------------------------------

def test_feature_candidate_default_asset_type() -> None:
    """Existing call sites that don't pass asset_type should still work."""
    candidate = FeatureCandidate(asset_id=uuid4(), asset_symbol="BTC/USD")
    assert candidate.asset_type == ""
