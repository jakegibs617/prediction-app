from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from app.alerts.rules import format_alert_payload, should_send_alert
from app.alerts.telegram import TelegramDeliveryResult, format_telegram_message, send_telegram_message
from app.db.pool import get_pool
from app.predictions.logic import PredictionRecord
from app.utils.time import get_utc_now


@dataclass(frozen=True)
class AlertRule:
    id: UUID
    name: str
    min_probability: Decimal
    max_horizon_hours: int
    channel_type: str
    destination: str
    is_active: bool


async def read_alert_rules() -> list[AlertRule]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, min_probability, max_horizon_hours, channel_type, destination, is_active
            FROM ops.alert_rules
            WHERE is_active = true
            ORDER BY created_at ASC
            """
        )
    return [
        AlertRule(
            id=row["id"],
            name=row["name"],
            min_probability=row["min_probability"],
            max_horizon_hours=row["max_horizon_hours"],
            channel_type=row["channel_type"],
            destination=row["destination"],
            is_active=row["is_active"],
        )
        for row in rows
    ]


async def check_already_alerted(prediction_id: UUID, alert_rule_id: UUID) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT 1
            FROM ops.alert_deliveries
            WHERE prediction_id = $1
              AND alert_rule_id = $2
              AND delivery_status = 'sent'
            """,
            prediction_id,
            alert_rule_id,
        )
    return bool(existing)


async def write_alert_delivery(
    *,
    prediction_id: UUID,
    alert_rule_id: UUID,
    delivery_status: str,
    last_error: str | None = None,
    provider_message_id: str | None = None,
) -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        delivery_id = await conn.fetchval(
            """
            INSERT INTO ops.alert_deliveries
                (prediction_id, alert_rule_id, delivery_status, attempt_count, last_attempt_at, last_error, provider_message_id)
            VALUES ($1, $2, $3, 1, $4, $5, $6)
            ON CONFLICT (prediction_id, alert_rule_id)
            DO UPDATE SET
                delivery_status = EXCLUDED.delivery_status,
                attempt_count = ops.alert_deliveries.attempt_count + 1,
                last_attempt_at = EXCLUDED.last_attempt_at,
                last_error = EXCLUDED.last_error,
                provider_message_id = COALESCE(EXCLUDED.provider_message_id, ops.alert_deliveries.provider_message_id)
            RETURNING id
            """,
            prediction_id,
            alert_rule_id,
            delivery_status,
            get_utc_now(),
            last_error,
            provider_message_id,
        )
    return delivery_id


async def process_prediction_alert(
    prediction: PredictionRecord,
    *,
    asset_symbol: str,
    target_metric: str,
    claim_type: str,
    rules: list[AlertRule] | None = None,
) -> list[TelegramDeliveryResult]:
    active_rules = rules if rules is not None else await read_alert_rules()
    results: list[TelegramDeliveryResult] = []

    for rule in active_rules:
        if rule.channel_type != "telegram":
            continue
        if not should_send_alert(
            prediction,
            max_horizon_hours=rule.max_horizon_hours,
        ):
            continue
        if await check_already_alerted(prediction.id, rule.id):
            continue

        payload = format_alert_payload(
            prediction,
            asset_symbol=asset_symbol,
            target_metric=target_metric,
            claim_type=claim_type,
        )
        message = format_telegram_message(payload)
        result = await send_telegram_message(rule.destination, message)
        await write_alert_delivery(
            prediction_id=prediction.id,
            alert_rule_id=rule.id,
            delivery_status="sent" if result.success else "failed",
            last_error=result.error,
            provider_message_id=result.provider_message_id,
        )
        results.append(result)

    return results
