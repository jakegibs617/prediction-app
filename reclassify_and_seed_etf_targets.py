"""Reclassify ETF assets to schema-compatible buckets and seed ETF prediction targets.

Mapping (matches how a trader thinks about each instrument):
  SPY, QQQ, TLT       -> equity     (broad market / tech / treasuries-as-equity-proxy)
  GLD, SLV, USO       -> commodity  (gold, silver, oil)

Targets seeded:
  SPY  positive next trading day
  QQQ  positive next trading day
  GLD  up >1.5% in 48h
  USO  up >2% in 48h          (WTI crude proxy)
"""
import asyncio
import json
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.db.pool import close_pool, get_pool, init_pool


ASSET_TYPE_MAP = {
    "SPY": "equity",
    "QQQ": "equity",
    "TLT": "equity",
    "GLD": "commodity",
    "SLV": "commodity",
    "USO": "commodity",
}

TARGETS = [
    {
        "name": "SPY positive next trading day",
        "asset_type": "equity",
        "asset_symbol": "SPY",
        "target_metric": "price_return_next_close",
        "horizon_hours": 24,
        "direction_rule": {
            "direction": "up",
            "metric": "price_return",
            "threshold": 0.0,
            "unit": "fraction",
        },
        "settlement_rule": {
            "type": "trading_day_close",
            "horizon": "next_n_bars",
            "n": 1,
            "calendar": "NYSE",
        },
    },
    {
        "name": "QQQ positive next trading day",
        "asset_type": "equity",
        "asset_symbol": "QQQ",
        "target_metric": "price_return_next_close",
        "horizon_hours": 24,
        "direction_rule": {
            "direction": "up",
            "metric": "price_return",
            "threshold": 0.0,
            "unit": "fraction",
        },
        "settlement_rule": {
            "type": "trading_day_close",
            "horizon": "next_n_bars",
            "n": 1,
            "calendar": "NYSE",
        },
    },
    {
        "name": "GLD up >1.5% in 48h",
        "asset_type": "commodity",
        "asset_symbol": "GLD",
        "target_metric": "price_return_48h",
        "horizon_hours": 48,
        "direction_rule": {
            "direction": "up",
            "metric": "price_return",
            "threshold": 0.015,
            "unit": "fraction",
        },
        "settlement_rule": {
            "type": "continuous",
            "horizon": "wall_clock_hours",
            "n": 48,
            "calendar": "none",
        },
    },
    {
        "name": "USO up >2% in 48h",
        "asset_type": "commodity",
        "asset_symbol": "USO",
        "target_metric": "price_return_48h",
        "horizon_hours": 48,
        "direction_rule": {
            "direction": "up",
            "metric": "price_return",
            "threshold": 0.02,
            "unit": "fraction",
        },
        "settlement_rule": {
            "type": "continuous",
            "horizon": "wall_clock_hours",
            "n": 48,
            "calendar": "none",
        },
    },
]


async def main() -> int:
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        # 1. Reclassify ETF assets so they match the schema's allowed asset_type literals.
        print("== reclassifying assets ==")
        for symbol, new_type in ASSET_TYPE_MAP.items():
            result = await conn.execute(
                "UPDATE market_data.assets SET asset_type = $1 "
                "WHERE symbol = $2 AND asset_type = 'etf'",
                new_type,
                symbol,
            )
            print(f"  {symbol:6s} -> {new_type:9s}  {result}")

        # 2. Show current asset_type breakdown.
        print("\n== assets by type ==")
        rows = await conn.fetch(
            "SELECT asset_type, count(*) AS n FROM market_data.assets "
            "GROUP BY asset_type ORDER BY asset_type"
        )
        for r in rows:
            print(f"  {r['asset_type']:10s} n={r['n']}")

        # 3. Seed ETF targets.
        print("\n== seeding targets ==")
        inserted = 0
        skipped = 0
        for tgt in TARGETS:
            asset_id = await conn.fetchval(
                "SELECT id FROM market_data.assets "
                "WHERE symbol = $1 AND asset_type = $2 LIMIT 1",
                tgt["asset_symbol"],
                tgt["asset_type"],
            )
            if not asset_id:
                print(f"  asset not found, skipping: {tgt['asset_symbol']} ({tgt['asset_type']})")
                skipped += 1
                continue
            existing = await conn.fetchval(
                "SELECT id FROM predictions.prediction_targets WHERE name = $1",
                tgt["name"],
            )
            if existing:
                print(f"  already seeded: {tgt['name']}")
                skipped += 1
                continue
            await conn.execute(
                "INSERT INTO predictions.prediction_targets "
                "(name, asset_type, target_metric, horizon_hours, direction_rule, settlement_rule, asset_id, is_active) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, true)",
                tgt["name"],
                tgt["asset_type"],
                tgt["target_metric"],
                tgt["horizon_hours"],
                json.dumps(tgt["direction_rule"]),
                json.dumps(tgt["settlement_rule"]),
                asset_id,
            )
            print(f"  inserted: {tgt['name']}")
            inserted += 1
        print(f"\ninserted={inserted} skipped={skipped}")

        # 4. List final targets.
        print("\n== all active targets ==")
        rows = await conn.fetch(
            "SELECT name, asset_type, horizon_hours, is_active "
            "FROM predictions.prediction_targets "
            "WHERE is_active = true ORDER BY asset_type, horizon_hours, name"
        )
        for r in rows:
            print(f"  {r['name']!r:50s} type={r['asset_type']:10s} h={r['horizon_hours']}h")

    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
