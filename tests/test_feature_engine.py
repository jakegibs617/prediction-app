from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.features.engine import (
    PriceBar,
    build_price_feature_snapshot,
    compute_rolling_window,
    read_prices_before,
)


def build_price_bars() -> list[PriceBar]:
    asset_id = uuid4()
    start = datetime(2026, 4, 17, 0, 0, tzinfo=UTC)
    return [
        PriceBar(
            asset_id=asset_id,
            source_record_id=uuid4(),
            bar_start_at=start + timedelta(hours=index),
            bar_end_at=start + timedelta(hours=index, minutes=59),
            close=100 + index,
        )
        for index in range(30)
    ]


def test_read_prices_before_filters_future_bars() -> None:
    bars = build_price_bars()
    cutoff = datetime(2026, 4, 17, 10, 30, tzinfo=UTC)
    filtered = read_prices_before(bars, cutoff)
    assert filtered[-1].bar_end_at < cutoff
    assert all(bar.bar_end_at < cutoff for bar in filtered)


def test_compute_rolling_window_is_deterministic() -> None:
    result = compute_rolling_window([1, 2, 3, 4, 5], window_size=3)
    assert result.values == [3, 4, 5]
    assert result.count == 3
    assert result.mean_value == 4


def test_build_price_feature_snapshot_uses_only_past_bars_and_tracks_lineage() -> None:
    bars = build_price_bars()
    asset_id = bars[0].asset_id
    cutoff = datetime(2026, 4, 18, 5, 0, tzinfo=UTC)

    snapshot = build_price_feature_snapshot(
        asset_id=asset_id,
        asset_symbol="BTC/USD",
        as_of_at=cutoff,
        price_bars=bars,
    )

    assert snapshot.asset_id == asset_id
    assert all(value.available_at < cutoff for value in snapshot.values)
    assert all(value.source_record_ids for value in snapshot.values)
    assert [value.feature_key for value in snapshot.values] == sorted(value.feature_key for value in snapshot.values)
