from __future__ import annotations

import httpx
import structlog

from app.model_client.base import ModelClient, ModelResponse

log = structlog.get_logger(__name__)

_ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicClient(ModelClient):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2000,
    ) -> ModelResponse:
        payload = {
            "model": self._model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": _ANTHROPIC_API_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        usage = data.get("usage", {})
        return ModelResponse(
            content=data["content"][0]["text"],
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=data.get("model", self._model),
        )
