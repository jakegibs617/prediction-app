# Model Configuration

This document covers the AI model strategy: which models to use by default, how to run them locally, and how to migrate to frontier models when the system is ready for production.

---

## Model Strategy

**Phase 1 — Development and validation: free local models via Ollama**

Use Ollama to run open-weight models entirely on your local machine. No API costs, no data leaving your network, unlimited inference for development and testing.

**Phase 2 — Staging / higher quality: free hosted inference (Groq)**

Once local testing is stable, switch to Groq's free tier for faster inference on the same open-weight models, still at no cost.

**Phase 3 — Production: frontier models (Claude, GPT-4o)**

When the heuristic baseline is validated and you need higher-quality structured reasoning, swap to a frontier model by changing two environment variables.

The model provider is fully abstracted behind a `ModelClient` interface — changing providers requires no code changes, only env var updates.

---

## Model Provider: Ollama (Default — Local, Free)

Ollama runs open-weight models locally via a Docker container or native install. It exposes an OpenAI-compatible REST API.

### Setup

```bash
# Option 1: Install natively (Mac/Linux)
curl -fsSL https://ollama.com/install.sh | sh

# Option 2: Docker
docker run -d -p 11434:11434 --name ollama ollama/ollama

# Pull a model
ollama pull llama3.2
ollama pull qwen2.5:7b
```

### Recommended Models (in order of preference)

| Model | Size | Use Case | Tool Use Support |
|---|---|---|---|
| `llama3.2` | 3B | Fast classification, normalization | Yes |
| `llama3.2:8b` | 8B | Balanced quality + speed | Yes |
| `qwen2.5:7b` | 7B | Strong instruction following | Yes |
| `mistral:7b` | 7B | Good reasoning, widely tested | Partial |
| `deepseek-r1:8b` | 8B | Chain-of-thought reasoning | No |

For this app, `llama3.2:8b` or `qwen2.5:7b` are the best starting points — both support structured output and tool use which the Prediction Engine requires.

### Ollama Environment Variables

```
AI_MODEL_PROVIDER=ollama
AI_MODEL_NAME=llama3.2:8b
OLLAMA_BASE_URL=http://localhost:11434
```

### Limitations

- No GPU: inference will be slow (5–30 seconds per prediction) on CPU-only machines. Acceptable for development; not suitable for high-frequency production use.
- Context window: most 7B models support 4K–8K tokens. Respect `MAX_AGENT_INPUT_TOKENS=6000` when on Ollama.
- Tool use reliability: smaller models occasionally fail to produce valid JSON tool calls. The agent layer must retry with a simplified prompt if the structured output is malformed.

---

## Model Provider: Groq (Free Hosted Inference)

Groq offers free-tier inference on open-weight models with very fast response times (low latency via custom hardware). Good bridge between local dev and production.

### Setup

1. Register at [https://console.groq.com](https://console.groq.com)
2. Create an API key under API Keys.
3. Set env vars:

```
AI_MODEL_PROVIDER=groq
AI_MODEL_NAME=llama-3.1-8b-instant
GROQ_API_KEY=REPLACE_WITH_GROQ_KEY
```

### Recommended Models on Groq

| Model | Groq model name | Notes |
|---|---|---|
| Llama 3.1 8B | `llama-3.1-8b-instant` | Fast, free, good for structured output |
| Llama 3.3 70B | `llama-3.3-70b-versatile` | Higher quality, still free tier |
| Mixtral 8x7B | `mixtral-8x7b-32768` | Long context window (32K) |

### Rate Limits (free tier)

- 30 requests per minute
- 14,400 requests per day
- Sufficient for development and light staging workloads

---

## Model Provider: Anthropic / Claude (Frontier — Production)

Claude models offer the best structured reasoning, reliable tool use, and long-context handling for this domain. Recommended when moving to production or when prediction quality matters more than cost.

### Setup

1. Register at [https://console.anthropic.com](https://console.anthropic.com)
2. Create an API key under API Keys.
3. Set env vars:

```
AI_MODEL_PROVIDER=anthropic
AI_MODEL_NAME=claude-sonnet-4-6
ANTHROPIC_API_KEY=REPLACE_WITH_ANTHROPIC_KEY
```

### Recommended Claude Models

| Model ID | Use Case | Notes |
|---|---|---|
| `claude-haiku-4-5-20251001` | High-volume normalization, fast classification | Lowest cost, fastest |
| `claude-sonnet-4-6` | Prediction engine, evidence generation | Best quality/cost balance |
| `claude-opus-4-7` | Complex reasoning, causation labeling | Highest quality, highest cost |

**Recommendation**: use Haiku for normalization and feature labeling; Sonnet for the Prediction Engine.

### Prompt Caching

Enable Anthropic prompt caching for the system prompt and prediction target definitions — these are static and re-sent on every prediction call. Caching reduces cost by ~90% for those tokens.

See the `claude-api` skill for implementation details.

---

## Model Provider: OpenAI / GPT-4o (Frontier Alternative)

```
AI_MODEL_PROVIDER=openai
AI_MODEL_NAME=gpt-4o-mini
OPENAI_API_KEY=REPLACE_WITH_OPENAI_KEY
```

`gpt-4o-mini` is the cost-effective option. `gpt-4o` for production quality.

---

## Context Windows and Costs

See `docs/context_and_cost_management.md` for the full reference table and compression strategy. In brief:

- The tightest constraint in the stack is **Groq `llama-3.1-8b-instant` at 8K tokens** — set `MAX_AGENT_INPUT_TOKENS=6000` when using it.
- All Anthropic Claude models support 200K token context — the most headroom for large feature snapshots.
- Ollama models have wide context windows but slow throughput on CPU-only machines.
- Set `MODEL_CONTEXT_WINDOW_TOKENS` and `MODEL_COST_INPUT_PER_M_USD` / `MODEL_COST_OUTPUT_PER_M_USD` in `.env` so the cost logger and context monitor work correctly for the active model.

## ModelClient Interface

All model providers are accessed through a single interface. Swapping providers is a two-variable env change.

```python
class ModelClient:
    def complete(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
    ) -> ModelResponse:
        """
        Send a completion request to the configured provider.
        Returns a structured response with text output and optional tool calls.
        """
        ...

    def complete_structured(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: type[BaseModel],
    ) -> BaseModel:
        """
        Request a JSON response matching the given Pydantic schema.
        Provider-agnostic structured output — uses tool call forcing on providers
        that don't support native JSON mode.
        """
        ...
```

The `ModelClient` is instantiated once per worker process and injected into agents. Changing `AI_MODEL_PROVIDER` and `AI_MODEL_NAME` in `.env` swaps the implementation without touching agent code.

---

## Migration Path: Local → Staging → Production

| Stage | Provider | Model | Rationale |
|---|---|---|---|
| Local dev | Ollama | `llama3.2:8b` | No cost, no network, fast iteration |
| Integration test | Ollama or Groq | `llama3.2:8b` or `llama-3.1-8b-instant` | Validate tool contracts work end-to-end |
| Staging / QA | Groq | `llama-3.3-70b-versatile` | Higher quality, still free |
| Production | Anthropic | `claude-haiku-4-5-20251001` + `claude-sonnet-4-6` | Reliable tool use, best prediction quality |

The baseline heuristic model (simple rule-based predictions) does not use an LLM at all and is the recommended first prediction engine regardless of provider — establish a measurable baseline before introducing LLM complexity.

---

## Configuration Reference

Add these to `.env` (see `.env.example` for full list):

```
AI_MODEL_PROVIDER=ollama
AI_MODEL_NAME=llama3.2:8b
OLLAMA_BASE_URL=http://localhost:11434
ANTHROPIC_API_KEY=REPLACE_WITH_ANTHROPIC_KEY
OPENAI_API_KEY=REPLACE_WITH_OPENAI_KEY
GROQ_API_KEY=REPLACE_WITH_GROQ_KEY
MAX_AGENT_INPUT_TOKENS=6000
MAX_AGENT_OUTPUT_TOKENS=2000
MAX_AGENT_TOOL_CALLS=20
```
