"""Run all connectors to seed the database with initial data."""
import asyncio
import structlog
from app.logging import configure_logging
from app.db.pool import init_pool, close_pool, get_pool
from app.connectors.alpha_vantage import AlphaVantageConnector
from app.connectors.cboe_options import CboeOptionsConnector
from app.connectors.cftc_cot import CftcCotConnector
from app.connectors.coingecko import CoinGeckoConnector
from app.connectors.eia import EiaConnector
from app.connectors.fear_greed import FearGreedConnector
from app.connectors.fred import FredConnector
from app.connectors.fred_calendar import FredCalendarConnector
from app.connectors.gdelt import GdeltConnector
from app.connectors.glasschain import GlasschainConnector
from app.connectors.newsapi import NewsApiConnector
from app.connectors.usgs import UsgsConnector

log = structlog.get_logger(__name__)

CONNECTORS = [
    ("CoinGecko", CoinGeckoConnector),
    ("FearGreed", FearGreedConnector),
    ("GDELT", GdeltConnector),
    ("USGS", UsgsConnector),
    ("FRED", FredConnector),
    ("FREDCalendar", FredCalendarConnector),
    ("EIA", EiaConnector),
    ("Glassnode", GlasschainConnector),
    ("CFTC COT", CftcCotConnector),
    ("Cboe Options", CboeOptionsConnector),
    ("NewsAPI", NewsApiConnector),
    ("AlphaVantage", AlphaVantageConnector),  # last — slow due to rate limits
]

async def main():
    configure_logging()
    await init_pool()
    for name, cls in CONNECTORS:
        print(f"--- Running {name} connector ---", flush=True)
        try:
            connector = cls()
            await connector.run()
            print(f"    {name}: OK", flush=True)
        except Exception as e:
            print(f"    {name}: FAILED — {e}", flush=True)
    
    pool = get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM ingestion.raw_source_records")
        print(f"\nTotal raw records ingested: {count}", flush=True)
    await close_pool()

asyncio.run(main())
