import asyncio
from app.db.pool import init_pool, close_pool, get_pool

async def main():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 'raw' as stage, (SELECT count(*) FROM ingestion.raw_source_records) as n
            UNION ALL SELECT 'norm', (SELECT count(*) FROM ingestion.normalized_events)
            UNION ALL SELECT 'feat', (SELECT count(*) FROM features.feature_snapshots)
            UNION ALL SELECT 'pred', (SELECT count(*) FROM predictions.predictions)
            UNION ALL SELECT 'eval', (SELECT count(*) FROM evaluation.evaluation_results)
            ORDER BY stage
        """)
        print('Counts:')
        for r in rows:
            print(f'  {r["stage"]}: {r["n"]}')
    await close_pool()

asyncio.run(main())
