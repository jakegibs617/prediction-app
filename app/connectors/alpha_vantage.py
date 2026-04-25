"""
Alpha Vantage connector — daily OHLC bars for equities and ETFs.

Endpoints used:
  TIME_SERIES_DAILY  — equities and ETFs (SPY, GLD, SLV, etc.)

Free tier: 25 requests/day, 5 requests/minute. We insert a 13-second sleep
between calls to stay comfortably under the per-minute cap.
Schedule this connector once per day — compact output returns the last 100 bars.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

import asyncio

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

BASE_URL = "https://www.alphavantage.co/query"


@dataclass
class AVAsset:
    symbol: str          # our internal symbol e.g. "SPY", "XAU/USD"
    name: str
    asset_type: str      # equity | etf | commodity | forex
    fetch_mode: Literal["equity", "forex"]
    av_symbol: str       # Alpha Vantage ticker e.g. "SPY"
    av_from: str = ""    # forex only: from_symbol e.g. "XAU"
    av_to: str = "USD"   # forex only: to_symbol


TRACKED_ASSETS: list[AVAsset] = [
    AVAsset("SPY", "SPDR S&P 500 ETF",          "etf", "equity", av_symbol="SPY"),
    AVAsset("QQQ", "Invesco Nasdaq-100 ETF",     "etf", "equity", av_symbol="QQQ"),
    AVAsset("GLD", "SPDR Gold Shares ETF",       "etf", "equity", av_symbol="GLD"),
    AVAsset("SLV", "iShares Silver Trust",       "etf", "equity", av_symbol="SLV"),
    AVAsset("USO", "US Oil Fund ETF",            "etf", "equity", av_symbol="USO"),
    AVAsset("TLT", "iShares 20yr Treasury ETF",  "etf", "equity", av_symbol="TLT"),
]


class AlphaVantageConnector(BaseConnector):
    source_name = "alpha_vantage"
    category = "market_data"
    base_url = BASE_URL
    auth_type = "api_key"
    trust_level = "unverified"
    rate_limit_per_minute = 5  # free tier

    def _api_key(self) -> str:
        key = settings.alpha_vantage_api_key
        if not key or key.startswith("REPLACE"):
            raise RuntimeError("ALPHA_VANTAGE_API_KEY is not set in .env")
        return key

    async def _fetch_equity_daily(self, av_symbol: str) -> dict:
        resp = await fetch_with_retry(
            BASE_URL,
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": av_symbol,
                "outputsize": "compact",
                "apikey": self._api_key(),
            },
            max_attempts=3,
        )
        data = resp.json()
        if "Error Message" in data or "Note" in data or "Information" in data:
            raise RuntimeError(
                f"Alpha Vantage error for {av_symbol}: "
                f"{data.get('Error Message') or data.get('Note') or data.get('Information')}"
            )
        series = data.get("Time Series (Daily)", {})
        if not series:
            raise RuntimeError(f"Alpha Vantage returned empty series for {av_symbol}")
        return series

    async def _fetch_fx_daily(self, from_sym: str, to_sym: str) -> dict:
        resp = await fetch_with_retry(
            BASE_URL,
            params={
                "function": "FX_DAILY",
                "from_symbol": from_sym,
                "to_symbol": to_sym,
                "outputsize": "compact",
                "apikey": self._api_key(),
            },
            max_attempts=3,
        )
        data = resp.json()
        if "Error Message" in data or "Note" in data:
            raise RuntimeError(f"Alpha Vantage FX error {from_sym}/{to_sym}: {data.get('Error Message') or data.get('Note')}")
        return data.get("Time Series FX (Daily)", {})

    async def _get_or_create_asset(self, conn, asset: AVAsset) -> UUID:
        row = await conn.fetchrow(
            "SELECT id FROM market_data.assets WHERE symbol = $1 AND asset_type = $2",
            asset.symbol, asset.asset_type,
        )
        if row:
            return row["id"]

        base = asset.av_from if asset.fetch_mode == "forex" else asset.av_symbol
        quote = asset.av_to if asset.fetch_mode == "forex" else "USD"

        asset_id = await conn.fetchval(
            """
            INSERT INTO market_data.assets (symbol, asset_type, name, base_currency, quote_currency)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            asset.symbol, asset.asset_type, asset.name, base, quote,
        )
        log.info("asset_created", symbol=asset.symbol, asset_id=str(asset_id))
        return asset_id

    async def _upsert_price_bar(self, conn, *, asset_id: UUID, source_id: UUID, date_str: str, bar: dict) -> bool:
        bar_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        bar_end = bar_start.replace(hour=23, minute=59, second=59)

        existing = await conn.fetchval(
            "SELECT 1 FROM market_data.price_bars WHERE asset_id=$1 AND bar_interval='1d' AND bar_start_at=$2",
            asset_id, bar_start,
        )
        if existing:
            return False

        open_  = bar.get("1. open") or bar.get("open")
        high   = bar.get("2. high") or bar.get("high")
        low    = bar.get("3. low")  or bar.get("low")
        close  = bar.get("4. close") or bar.get("close")
        volume = bar.get("5. volume")

        await conn.execute(
            """
            INSERT INTO market_data.price_bars
                (asset_id, source_id, bar_interval, bar_start_at, bar_end_at, open, high, low, close, volume)
            VALUES ($1,$2,'1d',$3,$4,$5,$6,$7,$8,$9)
            """,
            asset_id, source_id, bar_start, bar_end,
            str(open_), str(high), str(low), str(close),
            str(volume) if volume else None,
        )
        return True

    async def run(self) -> None:
        job_id = await acquire_job_lock("price_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            total_bars = 0
            total_raw = 0

            for i, asset in enumerate(TRACKED_ASSETS):
                if i > 0:
                    await asyncio.sleep(13)  # stay under 5 req/min free tier

                log.info("av_fetch_start", symbol=asset.symbol, mode=asset.fetch_mode)
                try:
                    if asset.fetch_mode == "equity":
                        series = await self._fetch_equity_daily(asset.av_symbol)
                    else:
                        series = await self._fetch_fx_daily(asset.av_from, asset.av_to)
                except Exception as exc:
                    log.error("av_fetch_failed", symbol=asset.symbol, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    asset_id = await self._get_or_create_asset(conn, asset)

                    for date_str, bar in series.items():
                        external_id = f"{asset.symbol}::{date_str}::1d"
                        raw_payload = {"date": date_str, **bar}
                        write_plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if write_plan.should_write:
                            await self.write_raw_record(
                                conn,
                                source_id=source_id,
                                external_id=external_id,
                                raw_payload=raw_payload,
                                source_recorded_at=datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc),
                                record_version=write_plan.record_version,
                                prior_record_id=write_plan.prior_record_id,
                                checksum=write_plan.checksum,
                            )
                            total_raw += 1

                        inserted = await self._upsert_price_bar(
                            conn, asset_id=asset_id, source_id=source_id,
                            date_str=date_str, bar=bar,
                        )
                        if inserted:
                            total_bars += 1

                log.info("av_asset_done", symbol=asset.symbol, bars_inserted=total_bars, raw_records=total_raw)

            await release_job_lock(job_id, "succeeded")
            log.info("av_run_complete", total_bars=total_bars, total_raw=total_raw)

        except Exception as exc:
            log.error("av_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
