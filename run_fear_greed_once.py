"""One-shot: run the Crypto Fear & Greed connector and report counts."""
import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.connectors.fear_greed import FearGreedConnector
from app.db.pool import close_pool, get_pool, init_pool


async def main() -> int:
    await init_pool()
    try:
        connector = FearGreedConnector()
        await connector.run()

        pool = get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM ingestion.raw_source_records r "
                "JOIN ops.api_sources s ON s.id = r.source_id "
                "WHERE s.name = 'alternative_me_fear_greed'"
            )
            sample = await conn.fetch(
                "SELECT r.external_id, r.raw_payload->>'value' AS value, "
                "       r.raw_payload->>'value_classification' AS classification, "
                "       r.source_recorded_at "
                "FROM ingestion.raw_source_records r "
                "JOIN ops.api_sources s ON s.id = r.source_id "
                "WHERE s.name = 'alternative_me_fear_greed' "
                "ORDER BY r.source_recorded_at DESC LIMIT 5"
            )
        print(f"alternative_me_fear_greed records in DB: {total}")
        print("most recent 5:")
        for row in sample:
            print(
                f"  {row['source_recorded_at']:%Y-%m-%d}  "
                f"value={row['value']:>3}  classification={row['classification']}"
            )
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
