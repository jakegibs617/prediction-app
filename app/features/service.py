from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from uuid import UUID, uuid4

from app.db.pool import get_pool
from app.features.engine import PriceBar, build_price_feature_snapshot
from app.predictions.contracts import FeatureSnapshot
from app.utils.time import get_utc_now

FEATURE_SET_NAME = "price-baseline"
FEATURE_SET_VERSION = "v1"


@dataclass(frozen=True)
class FeatureCandidate:
    asset_id: UUID
    asset_symbol: str


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
            SELECT id, symbol
            FROM market_data.assets
            WHERE is_active = true
            ORDER BY created_at ASC
            """
        )
    return [FeatureCandidate(asset_id=row["id"], asset_symbol=row["symbol"]) for row in rows]


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
    await write_feature_snapshot(snapshot, feature_set_id=feature_set_id)
    await write_feature_values(snapshot)
    await write_feature_lineage(snapshot)
    return snapshot
