from __future__ import annotations

import structlog

from app.config import settings
from app.db.pool import get_pool

_log = structlog.get_logger(__name__)


async def run_seed() -> None:
    """Idempotently insert reference data required before ingestion can run.

    Safe to call on every startup — all statements use ON CONFLICT to skip or update
    existing rows. Insertion order respects FK dependencies.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _seed_api_sources(conn)
            await _seed_assets(conn)
            await _seed_alert_rules(conn)
            await _seed_prediction_targets(conn)
    _log.info("db_seed_completed")


async def _seed_api_sources(conn) -> None:
    await conn.execute("""
        INSERT INTO ops.api_sources
            (id, name, category, base_url, auth_type, is_active, created_at,
             trust_level, rate_limit_per_minute, max_record_age_days)
        VALUES
            ('c1d49e67-5242-4796-a0e8-1443910813d1', 'coingecko',
             'market_data', 'https://api.coingecko.com/api/v3',
             'none', true, '2026-04-20 09:22:56.158783-04', 'unverified', 30, 90),

            ('b6b80684-fc60-4e69-9daf-85c392d66557', 'gdelt',
             'news', 'https://api.gdeltproject.org/api/v2/doc/doc',
             'none', true, '2026-04-20 09:22:57.746376-04', 'unverified', 10, 90),

            ('40130069-e496-44e7-841f-bdda868b9aef', 'usgs_earthquakes',
             'events', 'https://earthquake.usgs.gov/fdsnws/event/1/query',
             'none', true, '2026-04-20 09:24:13.740858-04', 'unverified', 30, 90),

            ('7efaeb82-d93e-42b7-aede-eca90ab022eb', 'fred',
             'macro', 'https://api.stlouisfed.org/fred',
             'api_key', true, '2026-04-20 09:24:14.169788-04', 'unverified', 120, 90),

            ('371393d1-7115-4e56-a894-9b5f8752374f', 'newsapi',
             'news', 'https://newsapi.org/v2',
             'api_key', true, '2026-04-20 09:24:23.686908-04', 'unverified', 10, 90),

            ('a79bda7b-f77f-493b-9b9e-c2d8abfe5b46', 'alpha_vantage',
             'market_data', 'https://www.alphavantage.co/query',
             'api_key', true, '2026-04-20 09:24:28.890744-04', 'unverified', 5, 90),

            ('b631292d-ae92-46c0-8853-4c021924a608', 'alternative_me_fear_greed',
             'macro', 'https://api.alternative.me',
             'none', true, '2026-04-25 07:52:32.616133-04', 'unverified', 60, 90),

            ('19d5dd00-2b81-449e-abbf-fb0565d08939', 'eia',
             'macro', 'https://api.eia.gov/v2',
             'api_key', true, '2026-04-25 07:59:28.800015-04', 'verified', 60, 90)
        ON CONFLICT DO NOTHING
    """)


async def _seed_assets(conn) -> None:
    await conn.execute("""
        INSERT INTO market_data.assets
            (id, symbol, asset_type, name, exchange, base_currency, quote_currency,
             is_active, created_at)
        VALUES
            ('f49ba139-f266-493a-b0e7-d084c09d4d50', 'BTC/USD', 'crypto',
             'Bitcoin', NULL, 'BTC', 'USD', true, '2026-04-20 09:22:56.485936-04'),

            ('9daf0f0b-378f-466b-9b32-26e9e61fcba3', 'ETH/USD', 'crypto',
             'Ethereum', NULL, 'ETH', 'USD', true, '2026-04-20 09:22:56.782263-04'),

            ('7298726f-4129-49bb-96ef-3c90ed993a23', 'XRP/USD', 'crypto',
             'XRP', NULL, 'XRP', 'USD', true, '2026-04-20 09:22:57.109643-04'),

            ('04f33c7b-130d-43af-9409-b310de2b757b', 'SOL/USD', 'crypto',
             'Solana', NULL, 'SOL', 'USD', true, '2026-04-20 09:22:57.397601-04'),

            ('7ede1f7d-0031-49bd-b0b0-1f56cc36d3c4', 'AVAX/USD', 'crypto',
             'Avalanche', NULL, 'AVAX', 'USD', true, '2026-04-20 09:22:57.705601-04'),

            ('1fd5c4b9-a031-4872-8cbe-248c7a622357', 'SPY', 'equity',
             'SPDR S&P 500 ETF', NULL, 'SPY', 'USD', true, '2026-04-20 09:24:29.244693-04'),

            ('31c080e9-8fd4-4f88-9080-091c2c670d8b', 'QQQ', 'equity',
             'Invesco Nasdaq-100 ETF', NULL, 'QQQ', 'USD', true, '2026-04-20 09:24:42.633043-04'),

            ('b940c6ba-76ad-41c9-80ac-b7bd9abdf1f6', 'TLT', 'equity',
             'iShares 20yr Treasury ETF', NULL, 'TLT', 'USD', true, '2026-04-20 09:25:36.222793-04'),

            ('586ed751-4c11-4433-aee6-b6e9fd290643', 'GLD', 'commodity',
             'SPDR Gold Shares ETF', NULL, 'GLD', 'USD', true, '2026-04-20 09:24:56.002394-04'),

            ('28809cfa-3d95-4d1b-891b-a79cf665ddc1', 'SLV', 'commodity',
             'iShares Silver Trust', NULL, 'SLV', 'USD', true, '2026-04-20 09:25:09.379571-04'),

            ('6728b7d1-64ab-41dd-863b-4cfc85470591', 'USO', 'commodity',
             'US Oil Fund ETF', NULL, 'USO', 'USD', true, '2026-04-20 09:25:22.772973-04'),

            ('0f604619-b410-4500-8b6e-480051e76bf9', 'SPY', 'etf',
             'SPDR S&P 500 ETF', NULL, 'SPY', 'USD', true, '2026-04-25 15:51:56.920476-04'),

            ('bb7778f6-1f5b-4b2f-894d-88643a4f425f', 'QQQ', 'etf',
             'Invesco Nasdaq-100 ETF', NULL, 'QQQ', 'USD', true, '2026-04-25 15:52:10.309927-04'),

            ('9e21ab9a-807d-49d5-adc6-c7eb4da4cbba', 'GLD', 'etf',
             'SPDR Gold Shares ETF', NULL, 'GLD', 'USD', true, '2026-04-25 15:52:23.678187-04'),

            ('969f41fa-fa77-4cbd-9fcb-af0161da5a68', 'SLV', 'etf',
             'iShares Silver Trust', NULL, 'SLV', 'USD', true, '2026-04-25 15:52:37.043443-04'),

            ('e803170c-5344-4076-a47b-ac580ad2fdef', 'USO', 'etf',
             'US Oil Fund ETF', NULL, 'USO', 'USD', true, '2026-04-25 15:52:50.398473-04'),

            ('af928c28-c623-48d9-8684-08b3356575db', 'TLT', 'etf',
             'iShares 20yr Treasury ETF', NULL, 'TLT', 'USD', true, '2026-04-25 15:53:03.749783-04')
        ON CONFLICT DO NOTHING
    """)


async def _seed_alert_rules(conn) -> None:
    # 001_init.sql already inserts a placeholder row with destination='REPLACE_WITH_TELEGRAM_CHAT_ID'.
    # DO UPDATE ensures the real chat ID and correct probability are applied on every startup.
    await conn.execute("""
        INSERT INTO ops.alert_rules
            (id, name, min_probability, max_horizon_hours, channel_type, destination,
             is_active, created_at)
        VALUES
            ('36681dd1-f4ea-479a-a9b8-a8c2ad5d5b5b',
             'default_telegram_high_confidence',
             0.65, 72, 'telegram', $1, true,
             '2026-04-20 09:17:09.981369-04')
        ON CONFLICT (name) DO UPDATE SET
            destination    = EXCLUDED.destination,
            min_probability = EXCLUDED.min_probability,
            updated_at     = now()
    """, settings.telegram_chat_id)


async def _seed_prediction_targets(conn) -> None:
    await conn.execute("""
        INSERT INTO predictions.prediction_targets
            (id, name, asset_type, target_metric, direction_rule, horizon_hours,
             settlement_rule, is_active, created_at, asset_id)
        VALUES
            ('1e16c83c-486f-45ce-a977-b97d398b0f58',
             'BTC/USD up >2% in 24h', 'crypto', 'price_return_24h',
             '{"unit":"fraction","metric":"price_return","direction":"up","threshold":0.02}'::jsonb,
             24,
             '{"n":24,"type":"continuous","horizon":"wall_clock_hours","calendar":"none"}'::jsonb,
             true, '2026-04-25 06:58:32.305955-04',
             'f49ba139-f266-493a-b0e7-d084c09d4d50'),

            ('3a5c35d3-6990-4726-9ea4-f17a6c39e4f2',
             'ETH/USD down >3% in 48h', 'crypto', 'price_return_48h',
             '{"unit":"fraction","metric":"price_return","direction":"down","threshold":0.03}'::jsonb,
             48,
             '{"n":48,"type":"continuous","horizon":"wall_clock_hours","calendar":"none"}'::jsonb,
             true, '2026-04-25 06:58:32.307271-04',
             '9daf0f0b-378f-466b-9b32-26e9e61fcba3'),

            ('be333f9d-4d71-432d-9af3-975c8bc318c4',
             'SPY positive next trading day', 'equity', 'price_return_next_close',
             '{"unit":"fraction","metric":"price_return","direction":"up","threshold":0.0}'::jsonb,
             24,
             '{"n":1,"type":"trading_day_close","horizon":"next_n_bars","calendar":"NYSE"}'::jsonb,
             true, '2026-04-25 07:01:55.095178-04',
             '1fd5c4b9-a031-4872-8cbe-248c7a622357'),

            ('84bca214-bcff-4b5b-8cf6-3271e9334d9e',
             'QQQ positive next trading day', 'equity', 'price_return_next_close',
             '{"unit":"fraction","metric":"price_return","direction":"up","threshold":0.0}'::jsonb,
             24,
             '{"n":1,"type":"trading_day_close","horizon":"next_n_bars","calendar":"NYSE"}'::jsonb,
             true, '2026-04-25 07:01:55.096143-04',
             '31c080e9-8fd4-4f88-9080-091c2c670d8b'),

            ('a932cfaf-da29-4b38-938b-56b7ec2fc670',
             'GLD up >1.5% in 48h', 'commodity', 'price_return_48h',
             '{"unit":"fraction","metric":"price_return","direction":"up","threshold":0.015}'::jsonb,
             48,
             '{"n":48,"type":"continuous","horizon":"wall_clock_hours","calendar":"none"}'::jsonb,
             true, '2026-04-25 07:01:55.096685-04',
             '586ed751-4c11-4433-aee6-b6e9fd290643'),

            ('37851c2f-3714-4c0e-8b15-5ad024812e78',
             'USO up >2% in 48h', 'commodity', 'price_return_48h',
             '{"unit":"fraction","metric":"price_return","direction":"up","threshold":0.02}'::jsonb,
             48,
             '{"n":48,"type":"continuous","horizon":"wall_clock_hours","calendar":"none"}'::jsonb,
             true, '2026-04-25 07:01:55.097118-04',
             '6728b7d1-64ab-41dd-863b-4cfc85470591')
        ON CONFLICT DO NOTHING
    """)
