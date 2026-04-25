from __future__ import annotations

import re

import structlog

from app.model_client.base import ModelClient
from app.normalization.contracts import ExtractionResult

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a financial news analyst. Extract structured metadata from the provided text.

Rules:
- You may only reference information explicitly present in the provided text.
- Do not invent, estimate, or assume values not stated in the text.
- If a field is not determinable from the text, use null or a neutral/zero value as appropriate.
- sentiment_score: -1.0 (very negative) to +1.0 (very positive), 0.0 = neutral.
- severity_score: 0.0 (routine) to 1.0 (extreme market-moving event).
- country_code: ISO 3166-1 alpha-2 (e.g. "US", "GB"), or null if not applicable.
"""

_INJECTION_PATTERNS = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?previous|disregard\s+(?:all\s+)?|"
    r"system\s*prompt|<\s*/?system\s*>|<\s*/?instruction\s*>|\[\s*system\s*\])"
)


def sanitize_for_prompt(text: str, max_chars: int = 500) -> str:
    cleaned = _INJECTION_PATTERNS.sub("", text)
    return cleaned.strip()[:max_chars]


async def extract_event_metadata(
    text: str,
    source_category: str,
    model_client: ModelClient,
) -> ExtractionResult:
    safe_text = sanitize_for_prompt(text)
    user_message = f"Source category: {source_category}\n\nText:\n{safe_text}"

    result = await model_client.complete_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        output_schema=ExtractionResult,
    )
    log.info(
        "extraction_complete",
        event_type=result.event_type,
        sentiment=result.sentiment_score,
        severity=result.severity_score,
    )
    return result
