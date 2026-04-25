from typing import Any
from uuid import UUID

import structlog

from app.db.pool import get_pool
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)


def structured_log(event: str, level: str = "info", **kwargs: Any) -> None:
    getattr(log, level)(event, **kwargs)


async def log_audit(
    entity_type: str,
    entity_id: UUID | str,
    action: str,
    actor_type: str = "system",
    details: dict | None = None,
    correlation_id: UUID | None = None,
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ops.audit_logs (entity_type, entity_id, action, correlation_id, actor_type, details, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            entity_type,
            str(entity_id),
            action,
            correlation_id,
            actor_type,
            details or {},
            get_utc_now(),
        )


async def get_asset_by_symbol(symbol: str, asset_type: str | None = None) -> dict | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        if asset_type:
            row = await conn.fetchrow(
                "SELECT * FROM market_data.assets WHERE symbol = $1 AND asset_type = $2",
                symbol, asset_type,
            )
        else:
            row = await conn.fetchrow(
                "SELECT * FROM market_data.assets WHERE symbol = $1 LIMIT 1",
                symbol,
            )
        return dict(row) if row else None
