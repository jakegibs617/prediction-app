"""Phase 1 smoke test: clear stale lock, reset only N quarantined rows,
run normalization once, report counts."""
import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.db.pool import close_pool, get_pool, init_pool  # noqa: E402

N = 10


async def main() -> int:
    await init_pool()
    pool = get_pool()

    async with pool.acquire() as conn:
        # 1. Clear stale running locks (idempotent)
        cleared = await conn.execute(
            "UPDATE ops.job_runs "
            "SET status = 'failed', finished_at = NOW(), error_summary = 'cleared stale lock' "
            "WHERE status = 'running'"
        )
        print(f"cleared_stale_locks: {cleared}")

        # 2. Reset only N quarantined rows -> pending
        reset = await conn.execute(
            f"UPDATE ingestion.raw_source_records "
            f"SET validation_status = 'pending', validation_errors = NULL "
            f"WHERE id IN ("
            f"  SELECT id FROM ingestion.raw_source_records "
            f"  WHERE validation_status = 'quarantined' "
            f"  LIMIT {N}"
            f")"
        )
        print(f"reset_to_pending: {reset}")

        # 3. Show statuses
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
