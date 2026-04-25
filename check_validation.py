from app.db.pool import init_pool, close_pool, get_pool
import asyncio

async def check():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        raw_count = await conn.fetchval("SELECT count(*) FROM ingestion.raw_source_records")
        norm_count = await conn.fetchval("SELECT count(*) FROM ingestion.normalized_events")
        print(f'Raw records: {raw_count}')
        print(f'Normalized events: {norm_count}')
        
        # Check what validation_status records have
        statuses = await conn.fetch("SELECT validation_status, count(*) as n FROM ingestion.raw_source_records GROUP BY validation_status ORDER BY n DESC")
        print('\nValidation statuses:')
        for s in statuses:
            print(f"  {s['validation_status']}: {s['n']}")
    await close_pool()

import asyncio
asyncio.run(check())
