from app.model_client._openai_compat import OpenAICompatClient


class GroqClient(OpenAICompatClient):
    def _extra_params(self) -> dict:
        return {"response_format": {"type": "json_object"}}
