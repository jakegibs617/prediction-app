"""Seed predictions.prediction_targets with the canonical crypto targets
from the project's progress.json design.

Targets:
  - BTC/USD up >2% in 24h
  - ETH/USD down >3% in 48h
"""
import asyncio
import json
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.db.pool import close_pool, get_pool, init_pool


TARGETS = [
    {
        "name": "BTC/USD up >2% in 24h",
        "asset_type": "crypto",
        "asset_symbol": "BTC/USD",
        "target_metric": "price_return_24h",
        "horizon_hours": 24,
        "direction_rule": {
            "direction": "up",
            "metric": "price_return",
            "threshold": 0.02,
            "unit": "fraction",
        },
        "settlement_rule": {
            "type": "continuous",
            "horizon": "wall_clock_hours",
            "n": 24,
            "calendar": "none",
        },
    },
    {
        "name": "ETH/USD down >3% in 48h",
        "asset_type": "crypto",
        "asset_symbol": "ETH/USD",
        "target_metric": "price_return_48h",
        "horizon_hours": 48,
        "direction_rule": {
            "direction": "down",
            "metric": "price_return",
            "threshold": 0.03,
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
    inserted = 0
    skipped = 0
    async with pool.acquire() as conn:
        for tgt in TARGETS:
            asset_id = await conn.fetchval(
                "SELECT id FROM market_data.assets "
                "WHERE symbol = $1 AND asset_type = $2 LIMIT 1",
                tgt["asset_symbol"],
                tgt["asset_type"],
            )
            if not asset_id:
                print(f"  asset not found, skipping: {tgt['asset_symbol']}")
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

        rows = await conn.fetch(
            "SELECT name, asset_type, horizon_hours, is_active "
            "FROM predictions.prediction_targets ORDER BY created_at"
        )
        print("\ncurrent targets:")
        for r in rows:
            print(f"  active={r['is_active']} {r['name']!r} type={r['asset_type']} h={r['horizon_hours']}h")

    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
