import asyncio, os, sys
os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
from app.db.pool import close_pool, get_pool, init_pool

async def main():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, min_probability, max_horizon_hours, channel_type, destination, is_active "
            "FROM ops.alert_rules ORDER BY created_at"
        )
        for r in rows:
            print(f"  active={r['is_active']} name={r['name']!r} prob>={r['min_probability']} "
                  f"horizon<={r['max_horizon_hours']}h channel={r['channel_type']} dest={r['destination']!r}")
    await close_pool()

asyncio.run(main())
