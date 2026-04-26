from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import structlog

from app.alerts.rules import confidence_label
from app.config import settings

log = structlog.get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org"
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class TelegramDeliveryResult:
    success: bool
    status_code: int | None
    provider_message_id: str | None
    error: str | None


def format_telegram_message(payload: dict[str, str]) -> str:
    label = confidence_label(float(payload["probability"]))
    lines = [
        f"<b>{label} prediction</b>",
        "",
        f"Asset: <b>{payload['asset_symbol']}</b>",
        f"Metric: {payload['target_metric']}",
        f"Outcome: {payload['predicted_outcome']}",
        f"Probability: <b>{payload['probability']}</b>",
        f"Horizon: {payload['forecast_horizon_hours']}h",
        "",
        "Evidence:",
        payload["evidence_summary"],
    ]
    if payload.get("claim_type_warning"):
        lines.extend(["", payload["claim_type_warning"]])
    lines.extend(["", f"Prediction ID: <code>{payload['prediction_id']}</code>"])
    return "\n".join(lines)


async def send_telegram_message(
    destination: str,
    message: str,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> TelegramDeliveryResult:
    token = settings.telegram_bot_token
    if not token:
        return TelegramDeliveryResult(
            success=False,
            status_code=None,
            provider_message_id=None,
            error="TELEGRAM_BOT_TOKEN is not configured",
        )
    if not destination:
        return TelegramDeliveryResult(
            success=False,
            status_code=None,
            provider_message_id=None,
            error="telegram destination is empty",
        )

    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": destination,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    last_error: str | None = None
    last_status_code: int | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
            last_status_code = response.status_code

            if response.status_code in _RETRY_STATUS_CODES:
                last_error = f"retryable status {response.status_code}"
            else:
                response.raise_for_status()
                body = response.json()
                message_id = body.get("result", {}).get("message_id")
                return TelegramDeliveryResult(
                    success=True,
                    status_code=response.status_code,
                    provider_message_id=str(message_id) if message_id is not None else None,
                    error=None,
                )
        except httpx.HTTPStatusError as exc:
            last_status_code = exc.response.status_code
            last_error = str(exc)
            if exc.response.status_code not in _RETRY_STATUS_CODES:
                break
        except httpx.RequestError as exc:
            last_error = str(exc)

        if attempt < max_attempts:
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))

    log.error("telegram_delivery_failed", destination=destination, status_code=last_status_code, error=last_error)
    return TelegramDeliveryResult(
        success=False,
        status_code=last_status_code,
        provider_message_id=None,
        error=last_error or "telegram delivery failed",
    )
