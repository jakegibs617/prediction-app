from app.config import settings
from app.model_client.base import ModelClient


def get_model_client() -> ModelClient:
    return _build(settings.ai_model_provider, settings.ai_model_name)


def get_cheap_model_client() -> ModelClient:
    return _build(settings.ai_model_provider_cheap, settings.ai_model_name_cheap)


def _build(provider: str, model: str) -> ModelClient:
    if provider == "ollama":
        from app.model_client.ollama import OllamaClient
        return OllamaClient(base_url=settings.ollama_base_url, api_key=None, model=model)

    if provider == "groq":
        from app.model_client.groq import GroqClient
        key = settings.groq_api_key
        if not key or key.startswith("REPLACE"):
            raise RuntimeError("GROQ_API_KEY is not configured")
        return GroqClient(base_url="https://api.groq.com/openai", api_key=key, model=model)

    if provider == "anthropic":
        from app.model_client.anthropic import AnthropicClient
        key = settings.anthropic_api_key
        if not key or key.startswith("REPLACE"):
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        return AnthropicClient(api_key=key, model=model)

    if provider == "openai":
        from app.model_client.openai import OpenAIClient
        key = settings.openai_api_key
        if not key or key.startswith("REPLACE"):
            raise RuntimeError("OPENAI_API_KEY is not configured")
        return OpenAIClient(base_url="https://api.openai.com", api_key=key, model=model)

    raise ValueError(f"Unknown AI_MODEL_PROVIDER: {provider!r}")
