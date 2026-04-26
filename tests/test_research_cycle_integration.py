from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from app.config import settings
from app.ops.orchestrator import ResearchOrchestrator


class TransactionAcquire:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class TransactionPool:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    def acquire(self) -> TransactionAcquire:
        return TransactionAcquire(self.conn)


async def _truncate_for_smoke_test(conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            TRUNCATE TABLE
            ops.alert_deliveries,
            evaluation.evaluation_results,
            predictions.prediction_status_history,
            predictions.predictions,
            predictions.prompt_versions,
            predictions.model_versions,
            predictions.prediction_targets,
            features.feature_lineage,
            features.feature_values,
            features.feature_snapshots,
            features.feature_sets,
            market_data.price_bars,
            market_data.assets,
            ingestion.normalized_events,
            ingestion.raw_source_records,
            ops.alert_rules,
            ops.api_sources,
            ops.job_runs
        RESTART IDENTITY CASCADE
        """
    )


@pytest.mark.asyncio
async def test_research_cycle_smoke_db_backed(monkeypatch) -> None:
    try:
        conn = await asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres unavailable for integration smoke test: {exc}")

    transaction = conn.transaction()
    await transaction.start()
    try:
        await _truncate_for_smoke_test(conn)

        test_pool = TransactionPool(conn)
        pool_targets = [
            "app.normalization.pipeline",
            "app.features.service",
            "app.predictions.service",
            "app.alerts.service",
            "app.alerts.pipeline",
            "app.evaluation.service",
            "app.ops.job_runs",
            "app.utils.logging",
        ]
        for target in pool_targets:
            monkeypatch.setattr(f"{target}.get_pool", lambda pool=test_pool: pool)

        issued_at = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
        evaluated_at = issued_at + timedelta(days=2)

        monkeypatch.setattr("app.predictions.service.get_utc_now", lambda: issued_at)
        monkeypatch.setattr("app.features.service.get_utc_now", lambda: issued_at)
        monkeypatch.setattr("app.evaluation.service.get_utc_now", lambda: evaluated_at)

        async def fake_send_telegram_message(destination: str, message: str, *, max_attempts: int = 3, base_delay: float = 1.0):
            from app.alerts.telegram import TelegramDeliveryResult

            return TelegramDeliveryResult(
                success=True,
                status_code=200,
                provider_message_id="integration-test-message",
                error=None,
            )

        async def fake_send_accuracy_report():
            return False

        monkeypatch.setattr("app.alerts.service.send_telegram_message", fake_send_telegram_message)
        monkeypatch.setattr("app.evaluation.pipeline.send_accuracy_report", fake_send_accuracy_report)

        source_id = uuid4()
        asset_id = uuid4()
        target_id = uuid4()

        await conn.execute(
            """
            INSERT INTO ops.api_sources (id, name, category, base_url, auth_type, trust_level, rate_limit_per_minute)
            VALUES ($1, 'integration_source', 'market_data', 'https://example.test', 'none', 'verified', 60)
            """,
            source_id,
        )
        await conn.execute(
            """
            INSERT INTO market_data.assets (id, symbol, asset_type, name, base_currency, quote_currency, is_active)
            VALUES ($1, 'BTC/USD', 'crypto', 'Bitcoin', 'BTC', 'USD', true)
            """,
            asset_id,
        )
        await conn.execute(
            """
            INSERT INTO predictions.prediction_targets
                (id, name, asset_type, target_metric, direction_rule, horizon_hours, settlement_rule, is_active)
            VALUES ($1, 'btc_up_2pct_24h', 'crypto', 'price_return', $2, 24, $3, true)
            """,
            target_id,
            '{"direction":"up","metric":"price_return","threshold":0.02,"unit":"fraction"}',
            '{"type":"continuous","horizon":"wall_clock_hours","n":24,"calendar":"none"}',
        )
        await conn.execute(
            """
            INSERT INTO ops.alert_rules
                (id, name, min_probability, max_horizon_hours, channel_type, destination, is_active)
            VALUES ($1, 'integration_telegram_rule', 0.85, 72, 'telegram', 'integration-chat', true)
            """,
            uuid4(),
        )
        historical_price_rows = []
        raw_rows = []
        for index in range(30):
            bar_start = issued_at - timedelta(hours=29 - index)
            bar_end = bar_start + timedelta(minutes=29)
            close = 130.0 - index
            ts_ms = int(bar_start.timestamp() * 1000)
            raw_id = uuid4()
            raw_rows.append(
                (
                    raw_id,
                    source_id,
                    f"BTC/USD::{ts_ms}::30m",
                    bar_start,
                    ts_ms,
                    close,
                )
            )
            historical_price_rows.append(
                (
                    uuid4(),
                    asset_id,
                    source_id,
                    bar_start,
                    bar_end,
                    close,
                    close + 1,
                    close - 1,
                    close,
                )
            )

        settlement_bar_start = issued_at + timedelta(hours=24) - timedelta(minutes=30)
        settlement_bar_end = issued_at + timedelta(hours=24) - timedelta(minutes=1)
        settlement_ts_ms = int(settlement_bar_start.timestamp() * 1000)
        raw_rows.append(
            (
                uuid4(),
                source_id,
                f"BTC/USD::{settlement_ts_ms}::30m",
                settlement_bar_start,
                settlement_ts_ms,
                105.0,
            )
        )

        await conn.executemany(
            """
            INSERT INTO ingestion.raw_source_records
                (id, source_id, external_id, record_version, source_recorded_at, ingested_at, raw_payload, checksum, validation_status)
            VALUES ($1, $2, $3, 1, $4, $4, jsonb_build_object('ts_ms', $5::bigint, 'close', $6::numeric), 'checksum', 'valid')
            """,
            raw_rows,
        )
        await conn.executemany(
            """
            INSERT INTO market_data.price_bars
                (id, asset_id, source_id, bar_interval, bar_start_at, bar_end_at, open, high, low, close)
            VALUES ($1, $2, $3, '30m', $4, $5, $6, $7, $8, $9)
            """,
            historical_price_rows
            + [
                (
                    uuid4(),
                    asset_id,
                    source_id,
                    settlement_bar_start,
                    settlement_bar_end,
                    105.0,
                    106.0,
                    104.0,
                    105.0,
                )
            ],
        )

        result = await ResearchOrchestrator().run_cycle()

        assert result.prediction_ran
        assert result.alerting_ran
        assert result.evaluation_ran

        prediction_count = await conn.fetchval("SELECT COUNT(*) FROM predictions.predictions")
        snapshot_count = await conn.fetchval("SELECT COUNT(*) FROM features.feature_snapshots")
        alert_count = await conn.fetchval("SELECT COUNT(*) FROM ops.alert_deliveries WHERE delivery_status = 'sent'")
        evaluation_row = await conn.fetchrow(
            """
            SELECT evaluation_state, actual_outcome, directional_correct
            FROM evaluation.evaluation_results
            """
        )
        job_statuses = await conn.fetch(
            "SELECT job_name, status FROM ops.job_runs ORDER BY started_at ASC"
        )

        assert snapshot_count == 1
        assert prediction_count == 1
        assert alert_count == 1
        assert evaluation_row["evaluation_state"] == "evaluated"
        assert evaluation_row["actual_outcome"] == "hit"
        assert evaluation_row["directional_correct"] is True
        assert [(row["job_name"], row["status"]) for row in job_statuses] == [
            ("normalization", "succeeded"),
            ("feature_generation", "succeeded"),
            ("prediction_run", "succeeded"),
            ("alert_check", "succeeded"),
            ("evaluation", "succeeded"),
        ]
    finally:
        await transaction.rollback()
        await conn.close()
