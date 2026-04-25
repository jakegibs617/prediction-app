from __future__ import annotations

import asyncio

import httpx
import structlog

from app.model_client.base import ModelClient, ModelResponse

log = structlog.get_logger(__name__)

_RATE_LIMIT_MAX_RETRIES = 6
_RATE_LIMIT_BASE_DELAY = 10.0  # seconds


class OpenAICompatClient(ModelClient):
    """Shared base for true OpenAI-compatible endpoints (OpenAI, Groq).

    Note: Ollama is NOT routed through here — its native /api/generate has
    a different schema. See OllamaClient for that path.
    """

    def __init__(self, base_url: str, api_key: str | None, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _extra_params(self) -> dict:
        """Override in subclasses to add provider-specific params (e.g. JSON mode)."""
        return {}

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2000,
    ) -> ModelResponse:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
            **self._extra_params(),
        }
        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )

            if resp.status_code == 429:
                if attempt >= _RATE_LIMIT_MAX_RETRIES:
                    resp.raise_for_status()
                # Honour Retry-After if provided, otherwise exponential backoff
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else _RATE_LIMIT_BASE_DELAY * (2 ** attempt)
                log.warning("rate_limited", attempt=attempt + 1, delay=delay, provider=self._base_url)
                await asyncio.sleep(delay)
                continue

            resp.raise_for_status()
            break

        data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return ModelResponse(
            content=choice["message"]["content"],
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", self._model),
        )
