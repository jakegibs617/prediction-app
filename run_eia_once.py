"""One-shot EIA connector run + report counts."""
import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.connectors.eia import EiaConnector
from app.db.pool import close_pool, get_pool, init_pool


async def main() -> int:
    await init_pool()
    try:
        await EiaConnector().run()
        pool = get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM ingestion.raw_source_records r "
                "JOIN ops.api_sources s ON s.id = r.source_id "
                "WHERE s.name = 'eia'"
            )
            sample = await conn.fetch(
                "SELECT r.raw_payload->>'series_id' AS series_id, "
                "       r.raw_payload->>'observation_date' AS obs_date, "
                "       r.raw_payload->>'value' AS value, "
                "       r.raw_payload->>'units' AS units "
                "FROM ingestion.raw_source_records r "
                "JOIN ops.api_sources s ON s.id = r.source_id "
                "WHERE s.name = 'eia' "
                "ORDER BY r.source_recorded_at DESC LIMIT 8"
            )
        print(f"eia records in DB: {total}")
        print("most recent 8:")
        for row in sample:
            print(
                f"  {row['obs_date']}  series={row['series_id']:<25s}  "
                f"value={row['value']:>10s}  units={row['units']}"
            )
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
