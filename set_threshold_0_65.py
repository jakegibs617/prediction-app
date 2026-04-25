import asyncio, os, sys
os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
from app.db.pool import close_pool, get_pool, init_pool


async def main():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ops.alert_rules SET min_probability = 0.65 "
            "WHERE name = $1",
            "default_telegram_high_confidence",
        )
        rows = await conn.fetch(
            "SELECT name, min_probability, max_horizon_hours, destination FROM ops.alert_rules"
        )
        for r in rows:
            print(
                f"  {r['name']}: prob>={r['min_probability']} "
                f"horizon<={r['max_horizon_hours']}h dest={r['destination']}"
            )
    await close_pool()


asyncio.run(main())
