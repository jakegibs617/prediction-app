from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog
from pydantic import BaseModel, ValidationError

log = structlog.get_logger(__name__)


@dataclass
class ModelResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str


class ModelClient(ABC):
    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2000,
    ) -> ModelResponse: ...

    async def complete_structured(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: type[BaseModel],
        max_retries: int = 2,
    ) -> BaseModel:
        schema_str = json.dumps(output_schema.model_json_schema(), indent=2)
        augmented_system = (
            f"{system_prompt}\n\n"
            f"Your response must be a single filled-in JSON object with real values — "
            f"NOT the schema definition itself. The schema to follow:\n{schema_str}\n"
            "Return ONLY the filled-in JSON object — no explanation, no markdown code fences, "
            "no schema keys like '$defs' or 'properties'."
        )

        last_exc: Exception | None = None
        current_user_message = user_message
        for attempt in range(max_retries + 1):
            try:
                response = await self.complete(augmented_system, current_user_message)
                content = response.content.strip()
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content.strip())
                # Literal newlines inside JSON strings are invalid; replace with spaces.
                # This is safe because JSON structure delimiters ({} [] : ,) don't
                # require newlines — they are optional whitespace only.
                content = content.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
                # Use raw_decode so trailing text after the JSON object is ignored
                decoder = json.JSONDecoder()
                data, _ = decoder.raw_decode(content)
                result = output_schema.model_validate(data)
                log.debug(
                    "structured_output_parsed",
                    schema=output_schema.__name__,
                    attempt=attempt + 1,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
                return result
            except (json.JSONDecodeError, ValidationError) as exc:
                last_exc = exc
                log.warning(
                    "structured_output_parse_failed",
                    schema=output_schema.__name__,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    error=str(exc),
                )
                current_user_message = (
                    f"Your previous response was not valid JSON or did not match the schema.\n"
                    f"Error: {exc}\n\n"
                    f"Original request:\n{user_message}\n\n"
                    "Return ONLY a valid JSON object matching the schema."
                )

        raise ValueError(
            f"Failed to parse {output_schema.__name__} after {max_retries + 1} attempts"
        ) from last_exc
