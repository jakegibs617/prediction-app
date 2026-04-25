from app.db.pool import init_pool, close_pool, get_pool
import asyncio

async def reset_quarantined():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        # Update quarantined records to pending
        rows_affected = await conn.fetchval(
            "UPDATE ingestion.raw_source_records "
            "SET validation_status = 'pending' "
            "WHERE validation_status = 'quarantined'"
        )
        print(f'Updated {rows_affected} quarantined records to pending')
        
        # Show current status
        statuses = await conn.fetch(
            "SELECT validation_status, count(*) as n FROM ingestion.raw_source_records GROUP BY validation_status ORDER BY n DESC"
        )
        print('\nValidation statuses:')
        for s in statuses:
            print(f"  {s['validation_status']}: {s['n']}")
    await close_pool()

import asyncio
asyncio.run(reset_quarantined())
