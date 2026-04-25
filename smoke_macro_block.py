import asyncio, os, sys
os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from app.db.pool import close_pool, init_pool
from app.predictions.llm_engine import _build_macro_block, fetch_macro_context


async def main() -> int:
    await init_pool()
    try:
        for asset_type in ("crypto", "equity", "commodity", "forex"):
            rows = await fetch_macro_context(asset_type)
            print(f"=== {asset_type} ({len(rows)} rows) ===")
            print(_build_macro_block(rows))
            print()
    finally:
        await close_pool()
    return 0


sys.exit(asyncio.run(main()))
