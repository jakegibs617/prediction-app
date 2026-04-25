import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.db.pool import close_pool, get_pool, init_pool


async def main() -> int:
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        print("== predictions.prediction_targets ==")
        rows = await conn.fetch(
            "SELECT name, asset_type, target_metric, horizon_hours, is_active, asset_id "
            "FROM predictions.prediction_targets ORDER BY created_at"
        )
        for r in rows:
            print(f"  active={r['is_active']} name={r['name']!r} asset_type={r['asset_type']!r} "
                  f"metric={r['target_metric']!r} h={r['horizon_hours']}h asset_id={r['asset_id']}")

        print("\n== market_data.assets ==")
        rows = await conn.fetch(
            "SELECT symbol, asset_type, is_active FROM market_data.assets ORDER BY created_at"
        )
        for r in rows:
            print(f"  active={r['is_active']} symbol={r['symbol']!r} asset_type={r['asset_type']!r}")

        print("\n== feature_snapshots per asset ==")
        rows = await conn.fetch(
            "SELECT a.symbol, a.asset_type, count(fs.id) AS snaps "
            "FROM market_data.assets a "
            "LEFT JOIN features.feature_snapshots fs ON fs.asset_id = a.id "
            "GROUP BY a.symbol, a.asset_type ORDER BY a.symbol"
        )
        for r in rows:
            print(f"  {r['symbol']:12s} type={r['asset_type']:10s} snaps={r['snaps']}")

        print("\n== price_bars per asset ==")
        rows = await conn.fetch(
            "SELECT a.symbol, count(pb.id) AS bars "
            "FROM market_data.assets a "
            "LEFT JOIN market_data.price_bars pb ON pb.asset_id = a.id "
            "GROUP BY a.symbol ORDER BY bars DESC"
        )
        for r in rows:
            print(f"  {r['symbol']:12s} bars={r['bars']}")
    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
