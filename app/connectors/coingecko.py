"""
CoinGecko connector — fetches daily OHLC price bars for configured crypto assets.

API used: /coins/{id}/ohlc?vs_currency=usd&days=1
No API key required for the public demo endpoint.
Rate limit: ~30 req/min on the free tier.
"""
import json
from datetime import datetime, timezone
from uuid import UUID

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

# (coingecko_id, symbol, display_name)
TRACKED_ASSETS: list[tuple[str, str, str]] = [
    ("bitcoin",       "BTC/USD",  "Bitcoin"),
    ("ethereum",      "ETH/USD",  "Ethereum"),
    ("ripple",        "XRP/USD",  "XRP"),
    ("solana",        "SOL/USD",  "Solana"),
    ("avalanche-2",   "AVAX/USD", "Avalanche"),
]


class CoinGeckoConnector(BaseConnector):
    source_name = "coingecko"
    category = "market_data"
    base_url = "https://api.coingecko.com/api/v3"
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 30

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
        return headers

    async def _get_or_create_asset(self, conn, symbol: str, name: str) -> UUID:
        row = await conn.fetchrow(
            "SELECT id FROM market_data.assets WHERE symbol = $1 AND asset_type = 'crypto'",
            symbol,
        )
        if row:
            return row["id"]

        base, quote = symbol.split("/") if "/" in symbol else (symbol, "USD")
        asset_id = await conn.fetchval(
            """
            INSERT INTO market_data.assets (symbol, asset_type, name, base_currency, quote_currency)
            VALUES ($1, 'crypto', $2, $3, $4)
            RETURNING id
            """,
            symbol, name, base, quote,
        )
        log.info("asset_created", symbol=symbol, asset_id=str(asset_id))
        return asset_id

    async def _fetch_ohlc(self, coin_id: str) -> list[list]:
        """Returns list of [timestamp_ms, open, high, low, close] from CoinGecko."""
        url = f"{self.base_url}/coins/{coin_id}/ohlc"
        resp = await fetch_with_retry(
            url,
            headers=self._headers(),
            params={"vs_currency": "usd", "days": "1"},
            max_attempts=3,
        )
        return resp.json()

    async def _upsert_price_bar(
        self,
        conn,
        *,
        asset_id: UUID,
        source_id: UUID,
        bar_start_ms: int,
        open_: float,
        high: float,
        low: float,
        close: float,
    ) -> bool:
        """Insert price bar; skip silently if it already exists. Returns True if inserted."""
        bar_start = datetime.fromtimestamp(bar_start_ms / 1000, tz=timezone.utc)
        # CoinGecko daily OHLC bars are 30-minute candles when days=1
        bar_end = datetime.fromtimestamp((bar_start_ms + 1_800_000) / 1000, tz=timezone.utc)

        existing = await conn.fetchval(
            """
            SELECT 1 FROM market_data.price_bars
            WHERE asset_id = $1 AND bar_interval = '30m' AND bar_start_at = $2
            """,
            asset_id, bar_start,
        )
        if existing:
            return False

        await conn.execute(
            """
            INSERT INTO market_data.price_bars
                (asset_id, source_id, bar_interval, bar_start_at, bar_end_at, open, high, low, close)
            VALUES ($1, $2, '30m', $3, $4, $5, $6, $7, $8)
            """,
            asset_id, source_id, bar_start, bar_end,
            str(open_), str(high), str(low), str(close),
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

            for coin_id, symbol, name in TRACKED_ASSETS:
                log.info("coingecko_fetch_start", coin_id=coin_id, symbol=symbol)
                try:
                    ohlc_data = await self._fetch_ohlc(coin_id)
                except Exception as exc:
                    log.error("coingecko_fetch_failed", coin_id=coin_id, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    asset_id = await self._get_or_create_asset(conn, symbol, name)

                    for bar in ohlc_data:
                        ts_ms, open_, high, low, close = bar
                        external_id = f"{symbol}::{ts_ms}::30m"
                        raw_payload = {"ts_ms": ts_ms, "open": open_, "high": high, "low": low, "close": close}
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
                                source_recorded_at=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                                record_version=write_plan.record_version,
                                prior_record_id=write_plan.prior_record_id,
                                checksum=write_plan.checksum,
                            )
                            total_raw += 1

                        inserted = await self._upsert_price_bar(
                            conn,
                            asset_id=asset_id,
                            source_id=source_id,
                            bar_start_ms=ts_ms,
                            open_=open_,
                            high=high,
                            low=low,
                            close=close,
                        )
                        if inserted:
                            total_bars += 1

                log.info(
                    "coingecko_asset_done",
                    symbol=symbol,
                    bars_inserted=total_bars,
                    raw_records=total_raw,
                )

            await release_job_lock(job_id, "succeeded")
            log.info("coingecko_run_complete", total_bars=total_bars, total_raw=total_raw)

        except Exception as exc:
            log.error("coingecko_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
