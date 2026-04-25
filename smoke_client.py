"""Smoke test: exercise OllamaClient.complete_structured the way the
normalization extractor does — with a Pydantic schema."""
import asyncio
import os
import sys

os.chdir(r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")
sys.path.insert(0, r"C:\Users\jakeg\OneDrive\Desktop\prediction-app")

from pydantic import BaseModel  # noqa: E402

from app.model_client.factory import get_cheap_model_client  # noqa: E402


class TinyResult(BaseModel):
    sentiment: str
    score: float


async def main() -> int:
    client = get_cheap_model_client()
    print(f"client_class={type(client).__name__} model={client._model}")
    try:
        out = await client.complete_structured(
            system_prompt="You extract sentiment from short text.",
            user_message="Stocks soared on strong earnings reports.",
            output_schema=TinyResult,
        )
    except Exception as e:
        print(f"COMPLETE_STRUCTURED_FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"OK sentiment={out.sentiment!r} score={out.score}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
