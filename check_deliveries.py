import asyncio, os, sys
os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
from app.db.pool import close_pool, get_pool, init_pool

async def main():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT delivery_status, count(*) AS n FROM ops.alert_deliveries "
            "GROUP BY delivery_status ORDER BY n DESC"
        )
        print("== alert deliveries by status ==")
        for r in rows:
            print(f"  {r['delivery_status']}: {r['n']}")

        rows = await conn.fetch(
            "SELECT ad.delivery_status, ad.attempt_count, ad.last_error, "
            "       ad.last_attempt_at, p.predicted_outcome, p.probability, "
            "       a.symbol "
            "FROM ops.alert_deliveries ad "
            "JOIN predictions.predictions p ON p.id = ad.prediction_id "
            "JOIN market_data.assets a ON a.id = p.asset_id "
            "ORDER BY ad.last_attempt_at DESC LIMIT 10"
        )
        print("\n== last 10 deliveries ==")
        for r in rows:
            err = r["last_error"][:80] if r["last_error"] else ""
            print(f"  {r['symbol']:9s} prob={float(r['probability']):.2f} "
                  f"status={r['delivery_status']:8s} attempts={r['attempt_count']} "
                  f"outcome={r['predicted_outcome']!r:6s} err={err!r}")
    await close_pool()

asyncio.run(main())
