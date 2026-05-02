from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from datetime import timezone
from uuid import UUID, uuid4

from app.db.pool import get_pool
from app.features.engine import (
    PriceBar,
    build_price_feature_snapshot,
    compute_cross_asset_btc_features,
    compute_rolling_correlations,
    compute_temporal_features,
    compute_volume_features,
)
from app.predictions.contracts import FeatureSnapshot, FeatureValue
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

FEATURE_SET_NAME = "price-baseline"
FEATURE_SET_VERSION = "v1"

BTC_SYMBOL = "BTC/USD"
# Altcoins that benefit from BTC lead features (BTC itself is excluded).
_ALTCOIN_SYMBOLS: frozenset[str] = frozenset({"ETH/USD", "SOL/USD", "AVAX/USD", "XRP/USD"})

# Symbols whose price bars are needed to compute cross-asset correlations.
_CORR_SYMBOLS: frozenset[str] = frozenset({"BTC/USD", "ETH/USD", "SPY", "GLD", "USO"})

# Macro features attached to each price-feature snapshot, keyed by asset_type.
# Each entry: (source_name, [(series_id, feature_key), ...]).
# feature_key gets a 'macro__' prefix so it sorts together in the prompt and
# is unmistakably a macro feature, not a price-derived one.
_CBOE_OPTIONS_ROUTES: tuple[tuple[str, str], ...] = (
    ("CBOE_PUT_CALL_TOTAL", "macro__cboe_total_put_call_ratio"),
    ("CBOE_PUT_CALL_INDEX", "macro__cboe_index_put_call_ratio"),
    ("CBOE_PUT_CALL_EQUITY", "macro__cboe_equity_put_call_ratio"),
    ("CBOE_VIX_SPOT_CLOSE", "macro__cboe_vix_spot_close"),
    ("CBOE_VIX_FRONT_FUTURE_SETTLE", "macro__cboe_vix_front_future_settle"),
    ("CBOE_VIX_SECOND_FUTURE_SETTLE", "macro__cboe_vix_second_future_settle"),
    ("CBOE_VIX_FRONT_PREMIUM_TO_SPOT", "macro__cboe_vix_front_premium_to_spot"),
    ("CBOE_VIX_1M_2M_FUTURES_SLOPE", "macro__cboe_vix_1m_2m_futures_slope"),
)

_MACRO_FEATURE_ROUTES: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {
    "crypto": [
        ("alternative_me_fear_greed", [("FEAR_GREED", "macro__fear_greed_index")]),
        ("glassnode", [
            ("GLASSNODE_EXCHANGE_NETFLOW_NATIVE_BTC", "macro__btc_exchange_netflow_native"),
            ("GLASSNODE_EXCHANGE_NETFLOW_NATIVE_ETH", "macro__eth_exchange_netflow_native"),
            ("GLASSNODE_MINERS_OUTFLOW_MULTIPLE_BTC", "macro__btc_miner_outflow_multiple"),
            ("GLASSNODE_LTH_NET_CHANGE_NATIVE_BTC", "macro__btc_lth_net_change_native"),
        ]),
        ("cftc_cot", [
            ("CFTC_COT_LEVERAGED_FUNDS_NET_BTC_CME", "macro__btc_cme_leveraged_funds_net_contracts"),
        ]),
        ("fred", [
            ("FEDFUNDS", "macro__fed_funds_rate"),
            ("DGS10", "macro__treasury_10y_yield"),
            ("DGS2",  "macro__treasury_2y_yield"),
        ]),
        ("cboe_options", list(_CBOE_OPTIONS_ROUTES)),
    ],
    "equity": [
        ("fred", [
            ("FEDFUNDS", "macro__fed_funds_rate"),
            ("DGS10", "macro__treasury_10y_yield"),
            ("DGS2",  "macro__treasury_2y_yield"),
            ("T10YIE", "macro__breakeven_inflation_10y"),
            ("UNRATE", "macro__unemployment_rate"),
        ]),
        ("cboe_options", list(_CBOE_OPTIONS_ROUTES)),
    ],
    "commodity": [
        ("eia", [
            ("RWTC", "macro__wti_crude_spot_usd"),
            ("WCESTUS1", "macro__crude_inventory_kbbl"),
            ("WGTSTUS1", "macro__gasoline_inventory_kbbl"),
            ("NW2_EPG0_SWO_R48_BCF", "macro__natgas_storage_bcf"),
        ]),
        ("cftc_cot", [
            ("CFTC_COT_MANAGED_MONEY_NET_GOLD", "macro__gold_managed_money_net_contracts"),
            ("CFTC_COT_MANAGED_MONEY_NET_WTI_CRUDE", "macro__wti_managed_money_net_contracts"),
        ]),
        ("fred", [
            # EIA RWTC is weekly; FRED DCOILWTICO is daily — keep both for
            # data-availability redundancy across different update cadences.
            ("DCOILWTICO", "macro__wti_crude_fred"),
            ("DGS10", "macro__treasury_10y_yield"),
            ("DGS2",  "macro__treasury_2y_yield"),
        ]),
        ("cboe_options", list(_CBOE_OPTIONS_ROUTES)),
    ],
    "forex": [
        ("fred", [
            ("FEDFUNDS", "macro__fed_funds_rate"),
            ("DGS10", "macro__treasury_10y_yield"),
            ("DGS2",  "macro__treasury_2y_yield"),
            ("T10YIE", "macro__breakeven_inflation_10y"),
        ]),
        ("cboe_options", list(_CBOE_OPTIONS_ROUTES)),
    ],
}


@dataclass(frozen=True)
class FeatureCandidate:
    asset_id: UUID
    asset_symbol: str
    asset_type: str = ""


async def get_or_create_feature_set() -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT id
            FROM features.feature_sets
            WHERE name = $1 AND version = $2
            """,
            FEATURE_SET_NAME,
            FEATURE_SET_VERSION,
        )
        if existing:
            return existing

        return await conn.fetchval(
            """
            INSERT INTO features.feature_sets (name, version, description)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            FEATURE_SET_NAME,
            FEATURE_SET_VERSION,
            "Rolling price features for heuristic predictions",
        )


async def read_feature_candidates() -> list[FeatureCandidate]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, symbol, asset_type
            FROM market_data.assets
            WHERE is_active = true
            ORDER BY created_at ASC
            """
        )
    candidates: list[FeatureCandidate] = []
    for row in rows:
        # row may be a real asyncpg.Record or a dict from a test fixture; use a
        # lookup that works for both and tolerates a missing asset_type.
        try:
            asset_type_val = row["asset_type"] or ""
        except (KeyError, TypeError):
            asset_type_val = ""
        candidates.append(
            FeatureCandidate(
                asset_id=row["id"],
                asset_symbol=row["symbol"],
                asset_type=asset_type_val,
            )
        )
    return candidates


async def read_price_bars(asset_id: UUID, cutoff_time) -> list[PriceBar]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pb.asset_id, pb.source_id, pb.bar_interval, pb.bar_start_at, pb.bar_end_at, pb.close, pb.volume, a.symbol
            FROM market_data.price_bars pb
            JOIN market_data.assets a ON a.id = pb.asset_id
            WHERE asset_id = $1
              AND pb.bar_end_at < $2
            ORDER BY pb.bar_end_at ASC
            """,
            asset_id,
            cutoff_time,
        )
        price_bars: list[PriceBar] = []
        for row in rows:
            external_id = _build_external_id(
                symbol=row["symbol"],
                bar_interval=row["bar_interval"],
                bar_start_at=row["bar_start_at"],
            )
            source_record_id = None
            if row["source_id"] is not None:
                source_record_id = await conn.fetchval(
                    """
                    SELECT id
                    FROM ingestion.raw_source_records
                    WHERE source_id = $1
                      AND external_id = $2
                    ORDER BY record_version DESC
                    LIMIT 1
                    """,
                    row["source_id"],
                    external_id,
                )
            try:
                volume = row["volume"]
            except (KeyError, TypeError):
                volume = None
            price_bars.append(
                PriceBar(
                    asset_id=row["asset_id"],
                    source_record_id=source_record_id or uuid4(),
                    bar_start_at=row["bar_start_at"],
                    bar_end_at=row["bar_end_at"],
                    close=float(row["close"]),
                    volume=float(volume) if volume is not None else None,
                )
            )
    return price_bars


def _build_external_id(*, symbol: str, bar_interval: str, bar_start_at) -> str:
    if bar_interval == "30m":
        ts_ms = int(bar_start_at.astimezone(timezone.utc).timestamp() * 1000)
        return f"{symbol}::{ts_ms}::30m"
    if bar_interval == "1d":
        return f"{symbol}::{bar_start_at.date().isoformat()}::1d"
    return f"{symbol}::{bar_start_at.isoformat()}::{bar_interval}"


async def feature_snapshot_exists(*, feature_set_id: UUID, asset_id: UUID, as_of_at) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT 1
            FROM features.feature_snapshots
            WHERE feature_set_id = $1
              AND asset_id = $2
              AND as_of_at = $3
            """,
            feature_set_id,
            asset_id,
            as_of_at,
        )
    return bool(existing)


async def write_feature_snapshot(snapshot: FeatureSnapshot, *, feature_set_id: UUID) -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO features.feature_snapshots (id, feature_set_id, asset_id, as_of_at, lineage_summary)
            VALUES ($1, $2, $3, $4, '{}'::jsonb)
            """,
            snapshot.snapshot_id,
            feature_set_id,
            snapshot.asset_id,
            snapshot.as_of_at,
        )
    return snapshot.snapshot_id


async def write_feature_values(snapshot: FeatureSnapshot) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO features.feature_values
                (id, snapshot_id, feature_key, feature_type, numeric_value, text_value, boolean_value, json_value, available_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            [
                (
                    uuid4(),
                    snapshot.snapshot_id,
                    value.feature_key,
                    value.feature_type,
                    value.numeric_value,
                    value.text_value,
                    value.boolean_value,
                    value.json_value,
                    value.available_at,
                )
                for value in snapshot.values
            ],
        )


async def write_feature_lineage(snapshot: FeatureSnapshot) -> None:
    source_record_ids = sorted(
        {source_id for value in snapshot.values for source_id in value.source_record_ids}
    )
    if not source_record_ids:
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        valid_source_record_ids = await conn.fetch(
            """
            SELECT id
            FROM ingestion.raw_source_records
            WHERE id = ANY($1::uuid[])
            """,
            source_record_ids,
        )
        existing_ids = [row["id"] for row in valid_source_record_ids]
        if not existing_ids:
            return
        await conn.executemany(
            """
            INSERT INTO features.feature_lineage (id, snapshot_id, source_record_id, note)
            VALUES ($1, $2, $3, $4)
            """,
            [
                (uuid4(), snapshot.snapshot_id, source_record_id, "price feature lineage")
                for source_record_id in existing_ids
            ],
        )


async def read_macro_feature_inputs(asset_type: str, cutoff_time) -> list[dict]:
    """Pull the most recent released macro observation for each (source,
    series) routed for this asset_type. cutoff_time enforces no-lookahead:
    we only consider observations whose released_at is on or before now.

    Returns a list of dicts with feature_key, value (float), source_record_id,
    available_at (datetime), source_name, series_id.
    """
    routes = _MACRO_FEATURE_ROUTES.get(asset_type, [])
    if not routes:
        return []

    pool = get_pool()
    out: list[dict] = []
    async with pool.acquire() as conn:
        for source_name, series_pairs in routes:
            for series_id, feature_key in series_pairs:
                row = await conn.fetchrow(
                    """
                    SELECT r.id                                AS source_record_id,
                           r.raw_payload->>'value'             AS value_text,
                           COALESCE(r.released_at, r.source_recorded_at) AS available_at
                    FROM ingestion.raw_source_records r
                    JOIN ops.api_sources s ON s.id = r.source_id
                    WHERE s.name = $1
                      AND r.raw_payload->>'series_id' = $2
                      AND COALESCE(r.released_at, r.source_recorded_at) <= $3
                    ORDER BY COALESCE(r.released_at, r.source_recorded_at) DESC
                    LIMIT 1
                    """,
                    source_name,
                    series_id,
                    cutoff_time,
                )
                if row is None:
                    continue
                value_text = row["value_text"]
                # EIA uses "." to represent unreleased / missing observations.
                if value_text in (None, "", "."):
                    continue
                try:
                    value_float = float(value_text)
                except (TypeError, ValueError):
                    continue
                out.append({
                    "feature_key": feature_key,
                    "numeric_value": value_float,
                    "source_record_id": row["source_record_id"],
                    "available_at": row["available_at"],
                    "source_name": source_name,
                    "series_id": series_id,
                })
    return out


def build_macro_feature_values(macro_inputs: list[dict]) -> list[FeatureValue]:
    """Convert macro_inputs (from read_macro_feature_inputs) into FeatureValue
    objects ready to merge into a snapshot."""
    out: list[FeatureValue] = []
    for m in macro_inputs:
        out.append(
            FeatureValue(
                feature_key=m["feature_key"],
                feature_type="numeric",
                numeric_value=m["numeric_value"],
                available_at=m["available_at"],
                source_record_ids=[m["source_record_id"]],
            )
        )
    return out


def compute_yield_curve_slope(macro_inputs: list[dict]) -> FeatureValue | None:
    """Derive yield curve slope (10Y − 2Y) from macro_inputs.

    available_at is the later of the two release dates so the feature is never
    stamped earlier than both underlying values are known.
    """
    dgs10 = next((m for m in macro_inputs if m["feature_key"] == "macro__treasury_10y_yield"), None)
    dgs2  = next((m for m in macro_inputs if m["feature_key"] == "macro__treasury_2y_yield"), None)
    if dgs10 is None or dgs2 is None:
        return None
    return FeatureValue(
        feature_key="macro__yield_curve_slope",
        feature_type="numeric",
        numeric_value=dgs10["numeric_value"] - dgs2["numeric_value"],
        available_at=max(dgs10["available_at"], dgs2["available_at"]),
        source_record_ids=[dgs10["source_record_id"], dgs2["source_record_id"]],
    )


# Maps economic_release event subtypes to their feature keys.
_CALENDAR_SUBTYPES: dict[str, str] = {
    "fomc": "macro__days_until_next_fomc",
    "cpi":  "macro__days_until_next_cpi",
    "ppi":  "macro__days_until_next_ppi",
    "nfp":  "macro__days_until_next_nfp",
}


async def read_calendar_events(cutoff_time) -> list[dict]:
    """Return upcoming economic_release events scheduled after cutoff_time.

    Joins to raw_source_records to get ingested_at, which is used as available_at
    for the derived days_until features so they never exceed the snapshot timestamp.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ne.event_subtype,
                   ne.event_occurred_at,
                   ne.source_record_id,
                   r.ingested_at AS available_at
            FROM ingestion.normalized_events ne
            JOIN ingestion.raw_source_records r ON r.id = ne.source_record_id
            WHERE ne.event_type = 'economic_release'
              AND ne.event_occurred_at > $1
            ORDER BY ne.event_occurred_at ASC
            """,
            cutoff_time,
        )
    return [dict(row) for row in rows]


def compute_calendar_features(events: list[dict], cutoff_time) -> list[FeatureValue]:
    """Compute days_until_next_* from a list of upcoming economic_release events.

    Takes the nearest upcoming occurrence of each tracked subtype. Events must be
    ordered ASC by event_occurred_at (as returned by read_calendar_events).
    """
    next_by_subtype: dict[str, dict] = {}
    for event in events:
        subtype = event.get("event_subtype")
        if subtype not in _CALENDAR_SUBTYPES:
            continue
        if subtype not in next_by_subtype:
            next_by_subtype[subtype] = event

    out: list[FeatureValue] = []
    for subtype, feature_key in _CALENDAR_SUBTYPES.items():
        event = next_by_subtype.get(subtype)
        if event is None:
            continue
        days_until = (event["event_occurred_at"] - cutoff_time).total_seconds() / 86400
        source_id = event.get("source_record_id")
        out.append(
            FeatureValue(
                feature_key=feature_key,
                feature_type="numeric",
                numeric_value=round(days_until, 2),
                available_at=event["available_at"],
                source_record_ids=[source_id] if source_id is not None else [],
            )
        )
    return out


async def read_asset_id_by_symbol(symbol: str) -> UUID | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM market_data.assets WHERE symbol = $1",
            symbol,
        )


async def read_bars_for_symbols(symbols: frozenset[str], cutoff_time) -> dict[str, list[PriceBar]]:
    """Fetch price bars for each symbol, keyed by symbol string."""
    bars_by_symbol: dict[str, list[PriceBar]] = {}
    for symbol in symbols:
        asset_id = await read_asset_id_by_symbol(symbol)
        if asset_id is None:
            continue
        bars = await read_price_bars(asset_id, cutoff_time)
        if bars:
            bars_by_symbol[symbol] = bars
    return bars_by_symbol


async def generate_features_for_asset(candidate: FeatureCandidate, *, as_of_at=None) -> FeatureSnapshot | None:
    cutoff_time = as_of_at or get_utc_now()
    feature_set_id = await get_or_create_feature_set()
    if await feature_snapshot_exists(feature_set_id=feature_set_id, asset_id=candidate.asset_id, as_of_at=cutoff_time):
        return None

    price_bars = await read_price_bars(candidate.asset_id, cutoff_time)
    if not price_bars:
        return None

    snapshot = build_price_feature_snapshot(
        asset_id=candidate.asset_id,
        asset_symbol=candidate.asset_symbol,
        as_of_at=cutoff_time,
        price_bars=price_bars,
        feature_set_name=f"{FEATURE_SET_NAME}-{FEATURE_SET_VERSION}",
    )

    # Splice macro features in, routed by asset_type. They share the same
    # snapshot_id and lineage machinery as price features.
    if candidate.asset_type:
        macro_inputs = await read_macro_feature_inputs(candidate.asset_type, cutoff_time)
        if not macro_inputs and candidate.asset_type in _MACRO_FEATURE_ROUTES:
            log.warning(
                "macro_inputs_empty",
                asset_symbol=candidate.asset_symbol,
                asset_type=candidate.asset_type,
            )
        if macro_inputs:
            macro_values = build_macro_feature_values(macro_inputs)
            slope_fv = compute_yield_curve_slope(macro_inputs)
            if slope_fv is not None:
                macro_values.append(slope_fv)
            merged = list(snapshot.values) + macro_values
            snapshot = FeatureSnapshot(
                snapshot_id=snapshot.snapshot_id,
                asset_id=snapshot.asset_id,
                asset_symbol=snapshot.asset_symbol,
                as_of_at=snapshot.as_of_at,
                feature_set_name=snapshot.feature_set_name,
                values=sorted(merged, key=lambda v: v.feature_key),
            )

    # Inject BTC lead-return features for crypto altcoins. BTC price action
    # leads altcoins by 15–60 min, so these are the highest-signal cross-asset
    # features we can compute without a new data source.
    if candidate.asset_type == "crypto" and candidate.asset_symbol in _ALTCOIN_SYMBOLS:
        btc_asset_id = await read_asset_id_by_symbol(BTC_SYMBOL)
        if btc_asset_id is not None:
            btc_bars = await read_price_bars(btc_asset_id, cutoff_time)
            btc_features = compute_cross_asset_btc_features(btc_bars, as_of_at=cutoff_time)
            if btc_features:
                snapshot = FeatureSnapshot(
                    snapshot_id=snapshot.snapshot_id,
                    asset_id=snapshot.asset_id,
                    asset_symbol=snapshot.asset_symbol,
                    as_of_at=snapshot.as_of_at,
                    feature_set_name=snapshot.feature_set_name,
                    values=sorted(list(snapshot.values) + btc_features, key=lambda v: v.feature_key),
                )

    # Calendar features (days until next FOMC/CPI/PPI/NFP) apply to all asset types.
    calendar_events = await read_calendar_events(cutoff_time)
    calendar_fvs = compute_calendar_features(calendar_events, cutoff_time)
    if calendar_fvs:
        snapshot = FeatureSnapshot(
            snapshot_id=snapshot.snapshot_id,
            asset_id=snapshot.asset_id,
            asset_symbol=snapshot.asset_symbol,
            as_of_at=snapshot.as_of_at,
            feature_set_name=snapshot.feature_set_name,
            values=sorted(list(snapshot.values) + calendar_fvs, key=lambda v: v.feature_key),
        )

    # Rolling cross-asset correlations — computed on daily closes to handle
    # different bar granularities (hourly crypto vs. daily equities).
    corr_bars = await read_bars_for_symbols(_CORR_SYMBOLS, cutoff_time)
    corr_features = compute_rolling_correlations(corr_bars, as_of_at=cutoff_time)
    if corr_features:
        snapshot = FeatureSnapshot(
            snapshot_id=snapshot.snapshot_id,
            asset_id=snapshot.asset_id,
            asset_symbol=snapshot.asset_symbol,
            as_of_at=snapshot.as_of_at,
            feature_set_name=snapshot.feature_set_name,
            values=sorted(list(snapshot.values) + corr_features, key=lambda v: v.feature_key),
        )

    # Volume-weighted features — volume column is populated by Alpha Vantage bars;
    # gracefully skipped when volume data is absent.
    volume_fvs = compute_volume_features(price_bars, as_of_at=cutoff_time)
    if volume_fvs:
        snapshot = FeatureSnapshot(
            snapshot_id=snapshot.snapshot_id,
            asset_id=snapshot.asset_id,
            asset_symbol=snapshot.asset_symbol,
            as_of_at=snapshot.as_of_at,
            feature_set_name=snapshot.feature_set_name,
            values=sorted(list(snapshot.values) + volume_fvs, key=lambda v: v.feature_key),
        )

    # Temporal and regime features — day of week, realized volatility, and
    # mean-reversion timing from the last large move.
    temporal_fvs = compute_temporal_features(price_bars, as_of_at=cutoff_time)
    if temporal_fvs:
        snapshot = FeatureSnapshot(
            snapshot_id=snapshot.snapshot_id,
            asset_id=snapshot.asset_id,
            asset_symbol=snapshot.asset_symbol,
            as_of_at=snapshot.as_of_at,
            feature_set_name=snapshot.feature_set_name,
            values=sorted(list(snapshot.values) + temporal_fvs, key=lambda v: v.feature_key),
        )

    await write_feature_snapshot(snapshot, feature_set_id=feature_set_id)
    await write_feature_values(snapshot)
    await write_feature_lineage(snapshot)
    return snapshot
