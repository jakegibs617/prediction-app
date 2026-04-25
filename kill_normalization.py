from app.db.pool import init_pool, close_pool, get_pool
import asyncio

async def kill_job():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE ops.job_runs SET status = 'failed', finished_at = NOW() WHERE job_name = 'normalization'")
        print('Killed normalization job')
        
        # Show remaining jobs
        rows = await conn.fetch('SELECT job_name, status, started_at FROM ops.job_runs ORDER BY started_at DESC')
        for r in rows:
            print(f"  {r['job_name']}: {r['status']}")
    await close_pool()

import asyncio
asyncio.run(kill_job())
