"""Ollama native API client.

Uses POST /api/generate with Ollama's prompt-shaped payload.
This is intentionally NOT a subclass of OpenAICompatClient because
the local Ollama install does not expose /v1/chat/completions.

Notes:
  - We pass `think: false` so reasoning models (qwen3.x) skip the chain-of-
    thought stage and emit the final answer directly. Without this they
    burn the entire token budget on a hidden 'thinking' field and return
    response="".
  - We use the dedicated `system` field instead of baking the system prompt
    into the user prompt, which is cleaner and saves a small number of tokens.
"""
from __future__ import annotations

import httpx
import structlog

from app.model_client.base import ModelClient, ModelResponse

log = structlog.get_logger(__name__)


class OllamaClient(ModelClient):
    def __init__(self, base_url: str, api_key: str | None, model: str) -> None:
        # api_key is unused for Ollama, kept in signature for factory compatibility.
        self._base_url = base_url.rstrip("/")
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
            "prompt": user_message,
            "stream": False,
            # Ollama's native JSON mode — constrains output to valid JSON.
            "format": "json",
            # Skip chain-of-thought for reasoning models (qwen3.x, etc.).
            # Non-reasoning models (mistral) ignore this flag.
            "think": False,
            "options": {
                "temperature": 0.1,
                "num_predict": max_tokens,
            },
        }

        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{self._base_url}/api/generate",
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        content = data.get("response", "")
        # Ollama exposes prompt_eval_count / eval_count as approximate token counts.
        input_tokens = int(data.get("prompt_eval_count", 0) or 0)
        output_tokens = int(data.get("eval_count", 0) or 0)
        return ModelResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=data.get("model", self._model),
        )
