from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from datetime import timezone
from uuid import UUID, uuid4

from app.db.pool import get_pool
from app.features.engine import PriceBar, build_price_feature_snapshot
from app.predictions.contracts import FeatureSnapshot, FeatureValue
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

FEATURE_SET_NAME = "price-baseline"
FEATURE_SET_VERSION = "v1"

# Macro features attached to each price-feature snapshot, keyed by asset_type.
# Each entry: (source_name, [(series_id, feature_key), ...]).
# feature_key gets a 'macro__' prefix so it sorts together in the prompt and
# is unmistakably a macro feature, not a price-derived one.
_MACRO_FEATURE_ROUTES: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {
    "crypto": [
        ("alternative_me_fear_greed", [("FEAR_GREED", "macro__fear_greed_index")]),
        ("fred", [
            ("FEDFUNDS", "macro__fed_funds_rate"),
            ("DGS10", "macro__treasury_10y_yield"),
        ]),
    ],
    "equity": [
        ("fred", [
            ("FEDFUNDS", "macro__fed_funds_rate"),
            ("DGS10", "macro__treasury_10y_yield"),
            ("T10YIE", "macro__breakeven_inflation_10y"),
            ("UNRATE", "macro__unemployment_rate"),
        ]),
    ],
    "commodity": [
        ("eia", [
            ("RWTC", "macro__wti_crude_spot_usd"),
            ("WCESTUS1", "macro__crude_inventory_kbbl"),
            ("WGTSTUS1", "macro__gasoline_inventory_kbbl"),
            ("NW2_EPG0_SWO_R48_BCF", "macro__natgas_storage_bcf"),
        ]),
        ("fred", [
            # EIA RWTC is weekly; FRED DCOILWTICO is daily — keep both for
            # data-availability redundancy across different update cadences.
            ("DCOILWTICO", "macro__wti_crude_fred"),
            ("DGS10", "macro__treasury_10y_yield"),
        ]),
    ],
    "forex": [
        ("fred", [
            ("FEDFUNDS", "macro__fed_funds_rate"),
            ("DGS10", "macro__treasury_10y_yield"),
            ("T10YIE", "macro__breakeven_inflation_10y"),
        ]),
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
            SELECT pb.asset_id, pb.source_id, pb.bar_interval, pb.bar_start_at, pb.bar_end_at, pb.close, a.symbol
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
            price_bars.append(
                PriceBar(
                    asset_id=row["asset_id"],
                    source_record_id=source_record_id or uuid4(),
                    bar_start_at=row["bar_start_at"],
                    bar_end_at=row["bar_end_at"],
                    close=float(row["close"]),
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
            merged = list(snapshot.values) + macro_values
            snapshot = FeatureSnapshot(
                snapshot_id=snapshot.snapshot_id,
                asset_id=snapshot.asset_id,
                asset_symbol=snapshot.asset_symbol,
                as_of_at=snapshot.as_of_at,
                feature_set_name=snapshot.feature_set_name,
                values=sorted(merged, key=lambda v: v.feature_key),
            )

    await write_feature_snapshot(snapshot, feature_set_id=feature_set_id)
    await write_feature_values(snapshot)
    await write_feature_lineage(snapshot)
    return snapshot
