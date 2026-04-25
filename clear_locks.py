from app.db.pool import init_pool, close_pool, get_pool
import asyncio

async def clear_locks():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        # Update all job runs to failed status to clear locks
        await conn.execute("UPDATE ops.job_runs SET status = 'failed', finished_at = NOW() WHERE status = 'running'")
        print('Cleared job locks')
        
        # Show remaining jobs
        rows = await conn.fetch('SELECT job_name, status, started_at FROM ops.job_runs ORDER BY started_at DESC')
        for r in rows:
            print(f"  {r['job_name']}: {r['status']}")
        await close_pool()

import asyncio
asyncio.run(clear_locks())
