import os
os.chdir('C:/Users/jakeg/OneDrive/Desktop/prediction-app')

from app.db.pool import init_pool, close_pool, get_pool
import asyncio

async def check_status():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        # Check raw records
        raw_count = await conn.fetchval('SELECT count(*) FROM ingestion.raw_source_records')
        print(f'Raw records: {raw_count}')
        
        # Check normalized events
        norm_count = await conn.fetchval('SELECT count(*) FROM ingestion.normalized_events')
        print(f'Normalized events: {norm_count}')
        
        # Check candidates
        candidates = await conn.fetch('SELECT category, count(*) as n FROM ingestion.raw_source_records GROUP BY category ORDER BY category')
        print('\nRaw records by category:')
        for c in candidates:
            print(f"  {c['category']}: {c['n']}")
        
        # Check feature snapshots
        feat_count = await conn.fetchval('SELECT count(*) FROM features.feature_snapshots')
        print(f'\nFeature snapshots: {feat_count}')
        
        # Check predictions
        pred_count = await conn.fetchval('SELECT count(*) FROM predictions.predictions')
        print(f'\nPredictions: {pred_count}')
        
        # Check normalized events by category
        norm_by_cat = await conn.fetch('SELECT category, count(*) as n FROM ingestion.normalized_events GROUP BY category ORDER BY category')
        print('\nNormalized events by category:')
        for c in norm_by_cat:
            print(f"  {c['category']}: {c['n']}")
        
        await close_pool()

import asyncio
asyncio.run(check_status())
