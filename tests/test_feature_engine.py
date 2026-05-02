from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.features.engine import (
    PriceBar,
    build_price_feature_snapshot,
    compute_cross_asset_btc_features,
    compute_rolling_correlations,
    compute_rolling_window,
    compute_temporal_features,
    compute_volume_features,
    read_prices_before,
)
from app.features.service import compute_calendar_features, compute_yield_curve_slope


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


def test_compute_cross_asset_btc_features_returns_both_returns() -> None:
    bars = build_price_bars()
    as_of_at = datetime(2026, 4, 18, 5, 0, tzinfo=UTC)

    features = compute_cross_asset_btc_features(bars, as_of_at=as_of_at)

    keys = {f.feature_key for f in features}
    assert "cross_asset__btc_return_1h" in keys
    assert "cross_asset__btc_return_24h" in keys
    for f in features:
        assert f.feature_type == "numeric"
        assert f.numeric_value is not None
        assert f.available_at < as_of_at
        assert f.source_record_ids


def test_compute_cross_asset_btc_features_empty_bars_returns_empty() -> None:
    features = compute_cross_asset_btc_features([], as_of_at=datetime(2026, 4, 18, tzinfo=UTC))
    assert features == []


def test_compute_cross_asset_btc_features_skips_unavailable_return() -> None:
    # Only 1 bar — enough for latest_close but not a 1h or 24h return
    asset_id = uuid4()
    single_bar = [
        PriceBar(
            asset_id=asset_id,
            source_record_id=uuid4(),
            bar_start_at=datetime(2026, 4, 17, 0, 0, tzinfo=UTC),
            bar_end_at=datetime(2026, 4, 17, 0, 59, tzinfo=UTC),
            close=100.0,
        )
    ]
    features = compute_cross_asset_btc_features(single_bar, as_of_at=datetime(2026, 4, 18, tzinfo=UTC))
    assert features == []


# --- compute_yield_curve_slope ---

def _macro_input(feature_key: str, value: float, available_at: datetime | None = None) -> dict:
    return {
        "feature_key": feature_key,
        "numeric_value": value,
        "source_record_id": uuid4(),
        "available_at": available_at or datetime(2026, 1, 1, tzinfo=UTC),
        "source_name": "fred",
        "series_id": feature_key,
    }


def test_compute_yield_curve_slope_normal_case() -> None:
    inputs = [
        _macro_input("macro__treasury_10y_yield", 4.5),
        _macro_input("macro__treasury_2y_yield", 4.2),
    ]
    fv = compute_yield_curve_slope(inputs)
    assert fv is not None
    assert fv.feature_key == "macro__yield_curve_slope"
    assert abs(fv.numeric_value - 0.3) < 1e-9
    assert fv.feature_type == "numeric"
    assert len(fv.source_record_ids) == 2


def test_compute_yield_curve_slope_inverted_curve() -> None:
    inputs = [
        _macro_input("macro__treasury_10y_yield", 4.0),
        _macro_input("macro__treasury_2y_yield", 4.8),
    ]
    fv = compute_yield_curve_slope(inputs)
    assert fv is not None
    assert fv.numeric_value < 0


def test_compute_yield_curve_slope_missing_2y_returns_none() -> None:
    inputs = [_macro_input("macro__treasury_10y_yield", 4.5)]
    assert compute_yield_curve_slope(inputs) is None


def test_compute_yield_curve_slope_missing_10y_returns_none() -> None:
    inputs = [_macro_input("macro__treasury_2y_yield", 4.2)]
    assert compute_yield_curve_slope(inputs) is None


def test_compute_yield_curve_slope_uses_later_available_at() -> None:
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    later   = datetime(2026, 1, 5, tzinfo=UTC)
    inputs = [
        _macro_input("macro__treasury_10y_yield", 4.5, available_at=earlier),
        _macro_input("macro__treasury_2y_yield",  4.2, available_at=later),
    ]
    fv = compute_yield_curve_slope(inputs)
    assert fv is not None
    assert fv.available_at == later


# --- compute_calendar_features ---

def _calendar_event(subtype: str, days_ahead: float, cutoff: datetime, ingested_hours_before: float = 1.0) -> dict:
    return {
        "event_subtype": subtype,
        "event_occurred_at": cutoff + timedelta(hours=days_ahead * 24),
        "source_record_id": uuid4(),
        "available_at": cutoff - timedelta(hours=ingested_hours_before),
    }


def test_compute_calendar_features_basic() -> None:
    cutoff = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    events = [
        _calendar_event("fomc", 6.0, cutoff),
        _calendar_event("cpi",  13.0, cutoff),
    ]
    features = compute_calendar_features(events, cutoff)
    by_key = {f.feature_key: f for f in features}

    assert "macro__days_until_next_fomc" in by_key
    assert abs(by_key["macro__days_until_next_fomc"].numeric_value - 6.0) < 0.01
    assert "macro__days_until_next_cpi" in by_key
    assert abs(by_key["macro__days_until_next_cpi"].numeric_value - 13.0) < 0.01
    for fv in features:
        assert fv.feature_type == "numeric"
        assert fv.available_at < cutoff


def test_compute_calendar_features_empty_events_returns_empty() -> None:
    cutoff = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    assert compute_calendar_features([], cutoff) == []


def test_compute_calendar_features_picks_nearest_occurrence() -> None:
    cutoff = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    events = [
        _calendar_event("fomc", 6.0,  cutoff),
        _calendar_event("fomc", 48.0, cutoff),
    ]
    features = compute_calendar_features(events, cutoff)
    fomc_fv = next(f for f in features if f.feature_key == "macro__days_until_next_fomc")
    assert abs(fomc_fv.numeric_value - 6.0) < 0.01


def test_compute_calendar_features_unknown_subtype_skipped() -> None:
    cutoff = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    events = [{"event_subtype": "gdp", "event_occurred_at": cutoff + timedelta(days=5), "source_record_id": uuid4(), "available_at": cutoff - timedelta(hours=1)}]
    assert compute_calendar_features(events, cutoff) == []


def test_compute_calendar_features_all_four_subtypes() -> None:
    cutoff = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    events = [
        _calendar_event("fomc", 5.0,  cutoff),
        _calendar_event("cpi",  10.0, cutoff),
        _calendar_event("ppi",  11.0, cutoff),
        _calendar_event("nfp",  4.0,  cutoff),
    ]
    features = compute_calendar_features(events, cutoff)
    keys = {f.feature_key for f in features}
    assert keys == {
        "macro__days_until_next_fomc",
        "macro__days_until_next_cpi",
        "macro__days_until_next_ppi",
        "macro__days_until_next_nfp",
    }


def test_compute_calendar_features_source_record_ids_populated() -> None:
    cutoff = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    record_id = uuid4()
    events = [{"event_subtype": "fomc", "event_occurred_at": cutoff + timedelta(days=7), "source_record_id": record_id, "available_at": cutoff - timedelta(hours=2)}]
    features = compute_calendar_features(events, cutoff)
    assert len(features) == 1
    assert features[0].source_record_ids == [record_id]


# --- compute_rolling_correlations ---

def _build_corr_bars(
    symbol: str,
    prices: list[float],
    start: datetime,
) -> list[PriceBar]:
    """Build one daily bar per price, each spanning a full calendar day."""
    asset_id = uuid4()
    bars = []
    for i, price in enumerate(prices):
        bar_start = start + timedelta(days=i)
        bar_end = bar_start + timedelta(hours=23, minutes=59)
        bars.append(
            PriceBar(
                asset_id=asset_id,
                source_record_id=uuid4(),
                bar_start_at=bar_start,
                bar_end_at=bar_end,
                close=price,
            )
        )
    return bars


def test_compute_rolling_correlations_perfect_positive_correlation() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    # ETH = BTC * 0.05 → identical returns → correlation = 1.0
    btc_prices = [100.0 + i * 2 for i in range(35)]
    eth_prices = [p * 0.05 for p in btc_prices]
    bars_by_symbol = {
        "BTC/USD": _build_corr_bars("BTC/USD", btc_prices, start),
        "ETH/USD": _build_corr_bars("ETH/USD", eth_prices, start),
    }
    as_of_at = start + timedelta(days=35)

    features = compute_rolling_correlations(bars_by_symbol, as_of_at=as_of_at)
    keys = {f.feature_key for f in features}

    assert "cross_asset__corr_btc_eth_7d" in keys
    assert "cross_asset__corr_btc_eth_30d" in keys

    for fv in features:
        if "btc_eth" in fv.feature_key:
            assert abs(fv.numeric_value - 1.0) < 1e-9, f"expected 1.0, got {fv.numeric_value}"


def test_compute_rolling_correlations_perfect_negative_correlation() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    n = 35
    # Alternating prices produce alternating returns that are anti-correlated.
    # GLD: 100, 101, 100, 101, ...  →  returns: +0.01, -0.0099, +0.01, ...
    # USO: 101, 100, 101, 100, ...  →  returns: -0.0099, +0.01, -0.0099, ...
    gld_prices = [100.0 + (i % 2) for i in range(n)]
    uso_prices = [100.0 + (1 - i % 2) for i in range(n)]
    bars_by_symbol = {
        "GLD": _build_corr_bars("GLD", gld_prices, start),
        "USO": _build_corr_bars("USO", uso_prices, start),
    }
    as_of_at = start + timedelta(days=n)

    features = compute_rolling_correlations(bars_by_symbol, as_of_at=as_of_at)
    corr_fvs = [f for f in features if "gld_uso" in f.feature_key]

    assert corr_fvs, "expected at least one GLD-USO correlation feature"
    for fv in corr_fvs:
        assert fv.numeric_value < 0, f"expected negative correlation, got {fv.numeric_value}"


def test_compute_rolling_correlations_anti_lookahead() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    prices = [100.0 + i for i in range(35)]
    bars_by_symbol = {
        "BTC/USD": _build_corr_bars("BTC/USD", prices, start),
        "ETH/USD": _build_corr_bars("ETH/USD", [p * 0.05 for p in prices], start),
    }
    as_of_at = start + timedelta(days=35)

    features = compute_rolling_correlations(bars_by_symbol, as_of_at=as_of_at)
    for fv in features:
        assert fv.available_at < as_of_at, f"{fv.feature_key}: available_at {fv.available_at} >= as_of_at {as_of_at}"


def test_compute_rolling_correlations_missing_symbol_skips_pair() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    prices = [100.0 + i for i in range(35)]
    # Only BTC — ETH and SPY are absent; GLD/USO pair also absent
    bars_by_symbol = {"BTC/USD": _build_corr_bars("BTC/USD", prices, start)}
    as_of_at = start + timedelta(days=35)

    features = compute_rolling_correlations(bars_by_symbol, as_of_at=as_of_at)
    assert features == []


def test_compute_rolling_correlations_insufficient_data_skips_window() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    # Only 3 bars — below _MIN_CORR_POINTS=4, so no correlations produced
    prices = [100.0, 102.0, 104.0]
    bars_by_symbol = {
        "BTC/USD": _build_corr_bars("BTC/USD", prices, start),
        "ETH/USD": _build_corr_bars("ETH/USD", [p * 0.05 for p in prices], start),
    }
    as_of_at = start + timedelta(days=10)

    features = compute_rolling_correlations(bars_by_symbol, as_of_at=as_of_at)
    assert features == []


def test_compute_rolling_correlations_expected_keys_present() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    prices = [100.0 + i for i in range(35)]
    bars_by_symbol = {
        "BTC/USD": _build_corr_bars("BTC/USD", prices, start),
        "ETH/USD": _build_corr_bars("ETH/USD", [p * 0.05 for p in prices], start),
        "SPY": _build_corr_bars("SPY", [p * 0.3 for p in prices], start),
    }
    as_of_at = start + timedelta(days=35)

    features = compute_rolling_correlations(bars_by_symbol, as_of_at=as_of_at)
    keys = {f.feature_key for f in features}
    assert "cross_asset__corr_btc_eth_7d" in keys
    assert "cross_asset__corr_btc_eth_30d" in keys
    assert "cross_asset__corr_btc_spy_7d" in keys
    assert "cross_asset__corr_btc_spy_30d" in keys


def test_compute_rolling_correlations_source_record_ids_populated() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    prices = [100.0 + i for i in range(35)]
    bars_by_symbol = {
        "BTC/USD": _build_corr_bars("BTC/USD", prices, start),
        "ETH/USD": _build_corr_bars("ETH/USD", [p * 0.05 for p in prices], start),
    }
    as_of_at = start + timedelta(days=35)

    features = compute_rolling_correlations(bars_by_symbol, as_of_at=as_of_at)
    for fv in features:
        assert fv.source_record_ids, f"{fv.feature_key}: source_record_ids must not be empty"


# --- compute_volume_features ---

def _build_volume_bars(
    volumes: list[float | None],
    closes: list[float] | None = None,
    *,
    start: datetime | None = None,
    asset_id: UUID | None = None,
) -> list[PriceBar]:
    """Build one hourly bar per entry; volume and close may be specified independently."""
    aid = asset_id or uuid4()
    t = start or datetime(2026, 4, 17, 0, 0, tzinfo=UTC)
    if closes is None:
        closes = [100.0 + i for i in range(len(volumes))]
    bars = []
    for i, (vol, close) in enumerate(zip(volumes, closes)):
        bars.append(
            PriceBar(
                asset_id=aid,
                source_record_id=uuid4(),
                bar_start_at=t + timedelta(hours=i),
                bar_end_at=t + timedelta(hours=i, minutes=59),
                close=close,
                volume=vol,
            )
        )
    return bars


def test_compute_volume_features_ratio_above_avg() -> None:
    # 19 baseline bars at volume=100, latest bar at volume=300 → ratio = 3.0
    volumes = [100.0] * 19 + [300.0]
    bars = _build_volume_bars(volumes)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_volume_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}

    assert "volume__ratio_20" in by_key
    ratio_fv = by_key["volume__ratio_20"]
    assert ratio_fv.feature_type == "numeric"
    assert abs(ratio_fv.numeric_value - 3.0) < 1e-9


def test_compute_volume_features_confirmed_move_true() -> None:
    # 1h return = +3% (>=2%), volume ratio = 3.0 (>=3x) → confirmed=True
    closes = [100.0] * 19 + [103.0]
    volumes = [100.0] * 19 + [300.0]
    bars = _build_volume_bars(volumes, closes)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_volume_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}

    assert "volume__confirmed_move" in by_key
    fv = by_key["volume__confirmed_move"]
    assert fv.feature_type == "boolean"
    assert fv.boolean_value is True


def test_compute_volume_features_confirmed_move_false_small_return() -> None:
    # 1h return = +0.5% (< 2%) even with 3x volume → confirmed=False
    closes = [100.0] * 19 + [100.5]
    volumes = [100.0] * 19 + [300.0]
    bars = _build_volume_bars(volumes, closes)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_volume_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}

    assert by_key["volume__confirmed_move"].boolean_value is False


def test_compute_volume_features_confirmed_move_false_low_volume() -> None:
    # 1h return = +3% but volume ratio = 1.5x (< 3x) → confirmed=False
    closes = [100.0] * 19 + [103.0]
    volumes = [100.0] * 19 + [150.0]
    bars = _build_volume_bars(volumes, closes)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_volume_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}

    assert by_key["volume__confirmed_move"].boolean_value is False


def test_compute_volume_features_no_volume_data_returns_empty() -> None:
    bars = _build_volume_bars([None] * 25)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)
    assert compute_volume_features(bars, as_of_at=as_of_at) == []


def test_compute_volume_features_anti_lookahead() -> None:
    volumes = [100.0] * 25
    bars = _build_volume_bars(volumes)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_volume_features(bars, as_of_at=as_of_at)
    for fv in features:
        assert fv.available_at < as_of_at, f"{fv.feature_key}: available_at {fv.available_at} >= as_of_at {as_of_at}"


def test_compute_volume_features_source_record_ids_populated() -> None:
    volumes = [100.0] * 25
    bars = _build_volume_bars(volumes)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_volume_features(bars, as_of_at=as_of_at)
    for fv in features:
        assert fv.source_record_ids, f"{fv.feature_key}: source_record_ids must not be empty"


def test_compute_volume_features_insufficient_volume_bars_returns_empty() -> None:
    # Only 1 bar with volume — need ≥2 to compute a ratio
    bars = _build_volume_bars([None] * 5 + [100.0])
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)
    assert compute_volume_features(bars, as_of_at=as_of_at) == []


# --- compute_temporal_features ---

def _build_flat_bars(
    n: int,
    *,
    price_step: float = 0.1,
    start: datetime | None = None,
) -> list[PriceBar]:
    """Build n hourly bars with a small monotonic price step (<1% per 24h)."""
    aid = uuid4()
    t = start or datetime(2026, 4, 20, 0, 0, tzinfo=UTC)  # Monday
    return [
        PriceBar(
            asset_id=aid,
            source_record_id=uuid4(),
            bar_start_at=t + timedelta(hours=i),
            bar_end_at=t + timedelta(hours=i, minutes=59),
            close=100.0 + i * price_step,
        )
        for i in range(n)
    ]


def test_compute_temporal_features_empty_bars_returns_empty() -> None:
    assert compute_temporal_features([], as_of_at=datetime(2026, 4, 20, tzinfo=UTC)) == []


def test_compute_temporal_features_day_of_week_text_value() -> None:
    # 2026-04-20 is a Monday (weekday() == 0)
    bars = _build_flat_bars(5)
    as_of_at = datetime(2026, 4, 20, 6, 0, tzinfo=UTC)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}

    assert "temporal__day_of_week" in by_key
    fv = by_key["temporal__day_of_week"]
    assert fv.feature_type == "text"
    assert fv.text_value == "Monday"


def test_compute_temporal_features_day_names_cover_all_days() -> None:
    # Use a Tuesday as_of_at to confirm day mapping is correct
    bars = _build_flat_bars(5, start=datetime(2026, 4, 21, 0, 0, tzinfo=UTC))
    as_of_at = datetime(2026, 4, 21, 6, 0, tzinfo=UTC)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}
    assert by_key["temporal__day_of_week"].text_value == "Tuesday"


def test_compute_temporal_features_realized_vol_positive() -> None:
    # 30 flat bars with slight price step → returns are non-zero, vol is positive
    bars = _build_flat_bars(30)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}

    assert "temporal__realized_vol_24h" in by_key
    fv = by_key["temporal__realized_vol_24h"]
    assert fv.feature_type == "numeric"
    assert fv.numeric_value is not None
    assert fv.numeric_value > 0


def test_compute_temporal_features_realized_vol_single_bar_skipped() -> None:
    # Only 1 bar before cutoff — can't compute a return, so no vol feature
    bars = _build_flat_bars(3)
    as_of_at = bars[0].bar_end_at + timedelta(minutes=1)  # only bars[0] is usable

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    keys = {f.feature_key for f in features}
    assert "temporal__realized_vol_24h" not in keys


def test_compute_temporal_features_days_since_large_move_detected() -> None:
    # build_price_bars() has prices 100–129 over 30 hourly bars.
    # bars[24].close=124, bars[0].close=100 → 24h return=24% ≥ 3%.
    # The most recent qualifying bar is bars[29]: bars[29]-bars[5]=124→129=~4%.
    bars = build_price_bars()
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    by_key = {f.feature_key: f for f in features}

    assert "temporal__days_since_large_move" in by_key
    fv = by_key["temporal__days_since_large_move"]
    assert fv.feature_type == "numeric"
    assert fv.numeric_value is not None
    assert fv.numeric_value >= 0


def test_compute_temporal_features_days_since_large_move_absent_when_no_large_move() -> None:
    # price_step=0.1 → 24h return ≈ 2.4% < 3% → no qualifying move
    bars = _build_flat_bars(50, price_step=0.1)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    keys = {f.feature_key for f in features}
    assert "temporal__days_since_large_move" not in keys


def test_compute_temporal_features_days_since_large_move_absent_too_few_bars() -> None:
    # Only 24 bars — can't compute a 24h return (need ≥25)
    bars = _build_flat_bars(24, price_step=5.0)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    keys = {f.feature_key for f in features}
    assert "temporal__days_since_large_move" not in keys


def test_compute_temporal_features_anti_lookahead() -> None:
    bars = _build_flat_bars(30)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    for fv in features:
        assert fv.available_at < as_of_at, (
            f"{fv.feature_key}: available_at {fv.available_at} >= as_of_at {as_of_at}"
        )


def test_compute_temporal_features_source_record_ids_populated() -> None:
    bars = _build_flat_bars(30)
    as_of_at = bars[-1].bar_end_at + timedelta(minutes=1)

    features = compute_temporal_features(bars, as_of_at=as_of_at)
    for fv in features:
        assert fv.source_record_ids, f"{fv.feature_key}: source_record_ids must not be empty"
