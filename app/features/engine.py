from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
