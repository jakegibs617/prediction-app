from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from statistics import mean, pstdev
from uuid import UUID, uuid4

from app.predictions.contracts import FeatureSnapshot, FeatureValue


@dataclass(frozen=True)
class PriceBar:
    asset_id: UUID
    source_record_id: UUID
    bar_start_at: datetime
    bar_end_at: datetime
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class WindowResult:
    values: list[float]
    count: int
    mean_value: float | None
    std_value: float | None


def read_prices_before(price_bars: list[PriceBar], cutoff_time: datetime) -> list[PriceBar]:
    return sorted(
        [bar for bar in price_bars if bar.bar_end_at < cutoff_time],
        key=lambda bar: bar.bar_end_at,
    )


def compute_rolling_window(
    values: list[float],
    *,
    window_size: int,
) -> WindowResult:
    window = values[-window_size:]
    if not window:
        return WindowResult(values=[], count=0, mean_value=None, std_value=None)

    std_value = 0.0 if len(window) == 1 else pstdev(window)
    return WindowResult(
        values=window,
        count=len(window),
        mean_value=mean(window),
        std_value=std_value,
    )


def _compute_return(price_bars: list[PriceBar], steps_back: int) -> tuple[float | None, list[UUID], datetime | None]:
    if len(price_bars) <= steps_back:
        return None, [], None

    latest = price_bars[-1]
    prior = price_bars[-1 - steps_back]
    if prior.close <= 0:
        raise ValueError("prior close must be positive")

    return (
        (latest.close - prior.close) / prior.close,
        [prior.source_record_id, latest.source_record_id],
        latest.bar_end_at,
    )


def build_price_feature_snapshot(
    *,
    asset_id: UUID,
    asset_symbol: str,
    as_of_at: datetime,
    price_bars: list[PriceBar],
    feature_set_name: str = "price-baseline-v1",
) -> FeatureSnapshot:
    usable_bars = read_prices_before(price_bars, as_of_at)
    if not usable_bars:
        raise ValueError("at least one price bar before the cutoff is required")

    closes = [bar.close for bar in usable_bars]
    latest_bar = usable_bars[-1]
    feature_values: list[FeatureValue] = [
        FeatureValue(
            feature_key="latest_close",
            feature_type="numeric",
            numeric_value=latest_bar.close,
            available_at=latest_bar.bar_end_at,
            source_record_ids=[latest_bar.source_record_id],
        )
    ]

    for label, steps_back in (("price_return_1h", 1), ("price_return_24h", 24)):
        return_value, lineage, available_at = _compute_return(usable_bars, steps_back)
        if return_value is not None and available_at is not None:
            feature_values.append(
                FeatureValue(
                    feature_key=label,
                    feature_type="numeric",
                    numeric_value=return_value,
                    available_at=available_at,
                    source_record_ids=lineage,
                )
            )

    rolling_24 = compute_rolling_window(closes, window_size=min(24, len(closes)))
    if rolling_24.mean_value is not None:
        feature_values.append(
            FeatureValue(
                feature_key="rolling_close_mean_24",
                feature_type="numeric",
                numeric_value=rolling_24.mean_value,
                available_at=latest_bar.bar_end_at,
                source_record_ids=[bar.source_record_id for bar in usable_bars[-rolling_24.count:]],
            )
        )
    if rolling_24.std_value is not None:
        feature_values.append(
            FeatureValue(
                feature_key="rolling_close_std_24",
                feature_type="numeric",
                numeric_value=rolling_24.std_value,
                available_at=latest_bar.bar_end_at,
                source_record_ids=[bar.source_record_id for bar in usable_bars[-rolling_24.count:]],
            )
        )

    return FeatureSnapshot(
        snapshot_id=uuid4(),
        asset_id=asset_id,
        asset_symbol=asset_symbol,
        as_of_at=as_of_at,
        feature_set_name=feature_set_name,
        values=sorted(feature_values, key=lambda value: value.feature_key),
    )


def compute_cross_asset_btc_features(
    btc_bars: list[PriceBar],
    *,
    as_of_at: datetime,
) -> list[FeatureValue]:
    """Return BTC lead-return features to merge into altcoin snapshots.

    BTC price action consistently leads altcoins by 15–60 minutes.
    Keys use the cross_asset__ prefix so they sort and render together.
    """
    usable = read_prices_before(btc_bars, as_of_at)
    if not usable:
        return []

    out: list[FeatureValue] = []
    for label, steps_back in (
        ("cross_asset__btc_return_1h", 1),
        ("cross_asset__btc_return_24h", 24),
    ):
        return_value, lineage, available_at = _compute_return(usable, steps_back)
        if return_value is not None and available_at is not None:
            out.append(
                FeatureValue(
                    feature_key=label,
                    feature_type="numeric",
                    numeric_value=return_value,
                    available_at=available_at,
                    source_record_ids=lineage,
                )
            )
    return out


# --- Volume-weighted features ---

_VOLUME_RATIO_WINDOW = 20  # bars for average volume baseline


def compute_volume_features(
    bars: list[PriceBar],
    *,
    as_of_at: datetime,
) -> list[FeatureValue]:
    """Compute volume ratio and a high-volume price-move confirmation flag.

    volume__ratio_20: current bar's volume divided by the mean of the previous
    20 bars. Values >1 mean above-average volume; >3 is exceptional.

    volume__confirmed_move: True when a ≥2% 1h price move is accompanied by
    ≥3× average volume — thin-volume moves are excluded from confirmation.

    Bars with NULL volume are skipped; returns [] when fewer than 2 volume bars
    are available before as_of_at.
    """
    usable = read_prices_before(bars, as_of_at)
    volume_bars = [b for b in usable if b.volume is not None and b.volume >= 0]
    if len(volume_bars) < 2:
        return []

    latest = volume_bars[-1]
    window = volume_bars[-_VOLUME_RATIO_WINDOW:]
    baseline_bars = window[:-1]
    avg_volume = mean(b.volume for b in baseline_bars)  # type: ignore[arg-type]
    if avg_volume <= 0:
        return []

    volume_ratio = latest.volume / avg_volume  # type: ignore[operator]
    out: list[FeatureValue] = []

    out.append(
        FeatureValue(
            feature_key="volume__ratio_20",
            feature_type="numeric",
            numeric_value=volume_ratio,
            available_at=latest.bar_end_at,
            source_record_ids=[b.source_record_id for b in window],
        )
    )

    return_1h, return_lineage, return_available_at = _compute_return(usable, 1)
    if return_1h is not None and return_available_at is not None:
        confirmed = abs(return_1h) >= 0.02 and volume_ratio >= 3.0
        combined_ids = list({*return_lineage, *(b.source_record_id for b in window)})
        out.append(
            FeatureValue(
                feature_key="volume__confirmed_move",
                feature_type="boolean",
                boolean_value=confirmed,
                available_at=max(latest.bar_end_at, return_available_at),
                source_record_ids=combined_ids,
            )
        )

    return out


# --- Temporal and regime features ---

_REALIZED_VOL_WINDOW = 24   # hourly bars for short-term vol
_LARGE_MOVE_THRESHOLD = 0.03  # 3% 24h return threshold

_DOW_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def compute_temporal_features(
    bars: list[PriceBar],
    *,
    as_of_at: datetime,
) -> list[FeatureValue]:
    """Compute temporal and regime features.

    temporal__day_of_week: UTC day name ("Monday"…"Sunday") as text, captures
    well-documented intraweek patterns (crypto Mon/Fri, equity Monday effect).

    temporal__realized_vol_24h: population std-dev of 1h returns over the last
    24 bars.  Distinct from rolling_close_std_24 (which is std of price levels);
    this measures short-term return volatility regardless of price scale.

    temporal__days_since_large_move: fractional days since the most recent bar
    whose 24h return exceeded ±3%.  Mean-reversion timing signal.  Omitted when
    no qualifying move is found in the available bar history.
    """
    usable = read_prices_before(bars, as_of_at)
    if not usable:
        return []

    latest = usable[-1]
    out: list[FeatureValue] = []

    out.append(
        FeatureValue(
            feature_key="temporal__day_of_week",
            feature_type="text",
            text_value=_DOW_NAMES[as_of_at.weekday()],
            available_at=latest.bar_end_at,
            source_record_ids=[latest.source_record_id],
        )
    )

    if len(usable) >= 2:
        window = usable[-_REALIZED_VOL_WINDOW:]
        closes = [b.close for b in window]
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        if len(returns) >= 2:
            out.append(
                FeatureValue(
                    feature_key="temporal__realized_vol_24h",
                    feature_type="numeric",
                    numeric_value=pstdev(returns),
                    available_at=window[-1].bar_end_at,
                    source_record_ids=[b.source_record_id for b in window],
                )
            )

    if len(usable) >= 25:
        for i in range(len(usable) - 1, 23, -1):
            prior_close = usable[i - 24].close
            if prior_close <= 0:
                continue
            if abs((usable[i].close - prior_close) / prior_close) >= _LARGE_MOVE_THRESHOLD:
                days_since = (latest.bar_end_at - usable[i].bar_end_at).total_seconds() / 86400
                out.append(
                    FeatureValue(
                        feature_key="temporal__days_since_large_move",
                        feature_type="numeric",
                        numeric_value=round(days_since, 2),
                        available_at=latest.bar_end_at,
                        source_record_ids=[usable[i].source_record_id, latest.source_record_id],
                    )
                )
                break

    return out


# --- Rolling cross-asset correlation ---

_MIN_CORR_POINTS = 4  # minimum aligned daily-close observations to compute a correlation

_SYMBOL_SLUGS: dict[str, str] = {
    "BTC/USD": "btc",
    "ETH/USD": "eth",
    "SOL/USD": "sol",
    "AVAX/USD": "avax",
    "XRP/USD": "xrp",
    "SPY": "spy",
    "GLD": "gld",
    "USO": "uso",
}

# Pairs to correlate; both symbols must have price bars in bars_by_symbol.
_CORR_PAIRS: tuple[tuple[str, str], ...] = (
    ("BTC/USD", "ETH/USD"),
    ("BTC/USD", "SPY"),
    ("GLD", "USO"),
)


def _symbol_to_slug(symbol: str) -> str:
    return _SYMBOL_SLUGS.get(symbol, symbol.lower().replace("/", "").replace("-", "_"))


def _daily_closes(bars: list[PriceBar]) -> dict[date, tuple[float, UUID, datetime]]:
    """Return the last close per UTC calendar date across a list of price bars."""
    by_date: dict[date, tuple[float, UUID, datetime]] = {}
    for bar in sorted(bars, key=lambda b: b.bar_end_at):
        by_date[bar.bar_end_at.date()] = (bar.close, bar.source_record_id, bar.bar_end_at)
    return by_date


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Pearson r for two equal-length sequences; None if undefined (zero variance)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    x_mean = mean(xs)
    y_mean = mean(ys)
    cov = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / n
    sx = pstdev(xs)
    sy = pstdev(ys)
    if sx == 0.0 or sy == 0.0:
        return None
    return cov / (sx * sy)


def compute_rolling_correlations(
    bars_by_symbol: dict[str, list[PriceBar]],
    *,
    as_of_at: datetime,
) -> list[FeatureValue]:
    """Compute 7-day and 30-day rolling Pearson correlations between key asset pairs.

    Uses daily closes to align assets that have different bar granularities (e.g.
    hourly crypto vs. daily equities). Correlations are computed on simple returns
    to remove price-level trends. available_at is the later of the two series'
    last bar_end_at in the window, preventing any lookahead.
    """
    out: list[FeatureValue] = []

    for sym_a, sym_b in _CORR_PAIRS:
        if sym_a not in bars_by_symbol or sym_b not in bars_by_symbol:
            continue

        bars_a = read_prices_before(bars_by_symbol[sym_a], as_of_at)
        bars_b = read_prices_before(bars_by_symbol[sym_b], as_of_at)

        daily_a = _daily_closes(bars_a)
        daily_b = _daily_closes(bars_b)
        common_dates = sorted(daily_a.keys() & daily_b.keys())

        slug_a = _symbol_to_slug(sym_a)
        slug_b = _symbol_to_slug(sym_b)

        for window_label, window_days in (("7d", 7), ("30d", 30)):
            window = common_dates[-window_days:]
            if len(window) < _MIN_CORR_POINTS:
                continue

            prices_a = [daily_a[d][0] for d in window]
            prices_b = [daily_b[d][0] for d in window]

            returns_a = [
                (prices_a[i] - prices_a[i - 1]) / prices_a[i - 1]
                for i in range(1, len(prices_a))
                if prices_a[i - 1] > 0
            ]
            returns_b = [
                (prices_b[i] - prices_b[i - 1]) / prices_b[i - 1]
                for i in range(1, len(prices_b))
                if prices_b[i - 1] > 0
            ]

            if len(returns_a) < _MIN_CORR_POINTS - 1 or len(returns_a) != len(returns_b):
                continue

            corr = _pearson_correlation(returns_a, returns_b)
            if corr is None:
                continue

            available_at = max(daily_a[window[-1]][2], daily_b[window[-1]][2])
            source_ids = list({daily_a[window[-1]][1], daily_b[window[-1]][1]})

            out.append(
                FeatureValue(
                    feature_key=f"cross_asset__corr_{slug_a}_{slug_b}_{window_label}",
                    feature_type="numeric",
                    numeric_value=corr,
                    available_at=available_at,
                    source_record_ids=source_ids,
                )
            )

    return out
