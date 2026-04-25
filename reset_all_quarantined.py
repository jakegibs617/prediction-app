import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.db.pool import close_pool, get_pool, init_pool  # noqa: E402


async def main() -> int:
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        cleared = await conn.execute(
            "UPDATE ops.job_runs "
            "SET status = 'failed', finished_at = NOW(), error_summary = 'cleared stale lock' "
            "WHERE status = 'running'"
        )
        print(f"cleared_stale_locks: {cleared}")

        reset = await conn.execute(
            "UPDATE ingestion.raw_source_records "
            "SET validation_status = 'pending', validation_errors = NULL "
            "WHERE validation_status = 'quarantined'"
        )
        print(f"reset_to_pending: {reset}")

        rows = await conn.fetch(
            "SELECT validation_status, count(*) AS n "
            "FROM ingestion.raw_source_records "
            "GROUP BY validation_status ORDER BY n DESC"
        )
        for r in rows:
            print(f"  status={r['validation_status']}: {r['n']}")

    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
