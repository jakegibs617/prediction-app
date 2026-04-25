"""Run normalization once and report counts. No DB-debug cruft at the end."""
import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.db.pool import close_pool, get_pool, init_pool  # noqa: E402
from app.normalization import NormalizationPipeline  # noqa: E402


async def main() -> int:
    await init_pool()
    try:
        normalizer = NormalizationPipeline()
        await normalizer.run()

        pool = get_pool()
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT count(*) FROM ingestion.raw_source_records"
            )
            norm = await conn.fetchval(
                "SELECT count(*) FROM ingestion.normalized_events"
            )
            statuses = await conn.fetch(
                "SELECT validation_status, count(*) AS n "
                "FROM ingestion.raw_source_records "
                "GROUP BY validation_status ORDER BY n DESC"
            )
        print(f"raw_records={raw}")
        print(f"normalized_events={norm}")
        for r in statuses:
            print(f"  status={r['validation_status']}: {r['n']}")
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
