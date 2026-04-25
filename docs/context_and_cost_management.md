# Context Management, Cost Control, and Hallucination Prevention

## Goals

- Prevent LLM hallucinations from entering prediction records or evidence summaries.
- Track token usage and cost per model call, per prediction, and per pipeline run.
- Detect when context is approaching a model's limit and compress intelligently before quality degrades.
- Reduce inference cost through tiered routing, prompt caching, and batching without sacrificing prediction quality.

---

## Model Context Windows and Costs

Each model has a hard context window limit. The system must know the limit for the active model to manage compression proactively.

### Context Window Reference

| Provider | Model | Context window | Tool use |
|---|---|---|---|
| Ollama | `llama3.2` (3B) | 128K tokens | Yes |
| Ollama | `llama3.2:8b` | 128K tokens | Yes |
| Ollama | `qwen2.5:7b` | 32K tokens | Yes |
| Ollama | `mistral:7b` | 32K tokens | Partial |
| Groq | `llama-3.1-8b-instant` | 8K tokens | Yes |
| Groq | `llama-3.3-70b-versatile` | 128K tokens | Yes |
| Anthropic | `claude-haiku-4-5-20251001` | 200K tokens | Yes |
| Anthropic | `claude-sonnet-4-6` | 200K tokens | Yes |
| Anthropic | `claude-opus-4-7` | 200K tokens | Yes |
| OpenAI | `gpt-4o` | 128K tokens | Yes |
| OpenAI | `gpt-4o-mini` | 128K tokens | Yes |

**Important**: Groq's `llama-3.1-8b-instant` has only an 8K context window — the tightest constraint in the stack. When this model is active, `MAX_AGENT_INPUT_TOKENS` must be set to 6000 or lower to leave room for output.

The active model's context window is read from the `MODEL_CONTEXT_WINDOW_TOKENS` env var (or auto-detected per provider). The system calculates utilization as a percentage before every LLM call.

### Cost Reference (approximate, USD per million tokens)

| Provider | Model | Input $/M | Output $/M | Relative cost |
|---|---|---|---|---|
| Ollama | any | $0 | $0 | Free |
| Groq | `llama-3.1-8b-instant` | $0 | $0 | Free (within limits) |
| Groq | `llama-3.3-70b-versatile` | $0 | $0 | Free (within limits) |
| Anthropic | `claude-haiku-4-5-20251001` | $0.25 | $1.25 | Very low |
| Anthropic | `claude-sonnet-4-6` | $3.00 | $15.00 | Moderate |
| Anthropic | `claude-opus-4-7` | $15.00 | $75.00 | High |
| OpenAI | `gpt-4o-mini` | $0.15 | $0.60 | Very low |
| OpenAI | `gpt-4o` | $2.50 | $10.00 | Moderate |

These are loaded from `MODEL_COST_INPUT_PER_M_USD` and `MODEL_COST_OUTPUT_PER_M_USD` env vars so they can be updated without code changes when providers change pricing.

---

## Hallucination Prevention

Hallucinations in this system mean the model invents facts not present in the feature snapshot — fabricated price levels, invented economic figures, or unsupported probability values. Because predictions are immutable and may trigger real alerts, hallucinated content is high-risk.

### Layer 1: Explicit System Prompt Constraint

The system prompt must include this instruction verbatim (or equivalent):

> "You may only reference data present in the EXTERNAL DATA block. Do not invent, estimate, extrapolate, or assume any numeric values not explicitly provided. If a relevant data point is missing, say so in the evidence summary rather than filling in a value."

### Layer 2: Low Temperature

Set model temperature to `0.0`–`0.2` for all prediction generation calls. Higher temperatures increase creativity but also increase hallucination rate for factual claims. Structured output generation (JSON, tool calls) should always use the lowest supported temperature.

```
MODEL_TEMPERATURE=0.1
```

### Layer 3: Output Schema Validation

All model outputs are parsed against a strict Pydantic schema before acting on them. If the model returns malformed JSON, omits required fields, or includes values outside the valid range (e.g. probability > 1.0), the output is rejected and the call is retried with a corrective follow-up prompt.

Maximum output validation retries: `MAX_OUTPUT_VALIDATION_RETRIES=2`. After that, the prediction is abandoned and the job is marked failed.

### Layer 4: Evidence Grounding Check

After the model generates an evidence summary, a post-processing step verifies that every numeric value cited in the summary appears in the feature snapshot. This is not a perfect check (text can paraphrase), but it catches obvious fabrications.

```python
def check_evidence_grounding(
    evidence_summary: str,
    snapshot: FeatureSnapshot
) -> GroundingResult:
    """
    Extract numeric values from evidence_summary.
    For each value, check if it appears (within tolerance) in snapshot.values.
    Return a GroundingResult with flagged_values list.
    Predictions with flagged_values are tagged with hallucination_risk = True
    and are not eligible for high-confidence alerts until manually reviewed.
    """
```

Predictions tagged `hallucination_risk = True` are stored normally but:
- Are excluded from Telegram alerts.
- Are surfaced in the Admin UI dashboard as requiring review.
- Are not counted in calibration metrics until reviewed.

### Layer 5: Probability Sanity Bounds

Flag (not reject) predictions where the model returns a probability below `HALLUCINATION_PROB_LOW` (default `0.05`) or above `HALLUCINATION_PROB_HIGH` (default `0.95`). Extreme probabilities from LLMs are frequently overconfident hallucinations.

Flagged predictions are stored with a `probability_extreme_flag = True` annotation and are excluded from alerts until reviewed. This is configurable — some legitimate targets may warrant extreme probabilities, so the bounds can be widened per prediction target.

### Layer 6: Self-Consistency Check (Optional, High-Cost)

For high-stakes predictions (probability >= `ALERT_MIN_PROBABILITY`), optionally run the same prompt N times (`SELF_CONSISTENCY_RUNS=3`) and compare the resulting probabilities. If the standard deviation of probabilities across runs exceeds `SELF_CONSISTENCY_MAX_STD` (default `0.10`), the prediction is flagged as high-variance and excluded from alerts.

This is expensive — each run costs additional tokens. Disabled by default; enable only when using frontier models on high-confidence targets.

```
SELF_CONSISTENCY_ENABLED=false
SELF_CONSISTENCY_RUNS=3
SELF_CONSISTENCY_MAX_STD=0.10
```

---

## Context Size Monitoring

### Thresholds

Two thresholds govern context management behavior, expressed as a percentage of the model's context window:

```
CONTEXT_WARNING_THRESHOLD_PCT=0.75    # 75% — begin pruning
CONTEXT_CRITICAL_THRESHOLD_PCT=0.90   # 90% — hard compress
```

Before every LLM call, compute:

```python
utilization = estimated_input_tokens / MODEL_CONTEXT_WINDOW_TOKENS
```

If utilization is below the warning threshold: proceed normally.
If at or above warning: enter **soft compression** (see below).
If at or above critical: enter **hard compression** (see below).

Token estimation uses a fast heuristic counter (4 characters ≈ 1 token for English text) rather than a full tokenizer, to avoid adding a tokenizer dependency per provider. This estimate is conservative — actual token counts are logged after the call.

### Soft Compression (75%–90% utilization)

Applied when context is at the warning threshold but below critical.

1. Sort feature values by a relevance score (higher = keep):
   - Features in the active feature set for the prediction target: +10
   - Features available within 1 hour of `issuance_time`: +5
   - Features with non-null numeric values: +3
   - Features whose key appears in prior prediction evidence summaries for this asset: +2
2. Drop the lowest-scoring features until utilization drops below the warning threshold.
3. Replace dropped features with a one-line summary: `"[N features omitted — low relevance]"`.
4. Log at `WARNING` with compression ratio and list of omitted feature keys.

### Hard Compression (>= 90% utilization)

Applied when context is at or above the critical threshold. More aggressive.

1. Keep only the top `MAX_FEATURES_CRITICAL` features (default: 10) by relevance score.
2. Compress the news/event context: keep only the 3 most recent events per event type.
3. Summarize the full evidence context in a single preceding pass using a cheap model (Haiku or llama3.2:3b): `"Summarize the following market context in 3 sentences, preserving key numeric values."` — store the summary as the compressed context.
4. Replace the full feature block with the compressed summary.
5. Log at `WARNING` with `compression_type=hard`, original token count, compressed token count, and compression ratio.
6. Tag the resulting prediction with `context_compressed = True` in the `rationale` JSONB field so it is identifiable in reporting.

### Context Compression Across Multi-Turn Agent Calls

For the Prediction Engine's iterative reasoning loop (up to `MAX_AGENT_ITERATIONS` passes), tool call results accumulate in the conversation history. Apply a sliding window:

- Keep the last `CONTEXT_KEEP_TOOL_RESULTS` tool call results in full (default: 5).
- For older tool results, replace the full result with a one-line summary of the outcome.
- Never drop the system prompt or the initial user message.

```
CONTEXT_KEEP_TOOL_RESULTS=5
MAX_FEATURES_CRITICAL=10
```

---

## Cost Logging

Every LLM call is logged to `ops.model_usage_log` (new table — see schema addition below) and emitted as a structured log line.

### Log Line (additional fields for LLM calls)

The standard log JSON schema gains these fields when `agent_type = llm_call`:

```json
{
  "timestamp": "...",
  "level": "INFO",
  "correlation_id": "...",
  "agent": "prediction_engine",
  "message": "llm call completed",
  "context": {
    "model_provider": "anthropic",
    "model_name": "claude-sonnet-4-6",
    "input_tokens": 2847,
    "output_tokens": 312,
    "context_utilization_pct": 68.4,
    "compression_applied": false,
    "cost_usd": 0.013251,
    "duration_ms": 1240
  }
}
```

`cost_usd` is computed as:
```
cost = (input_tokens / 1_000_000 * MODEL_COST_INPUT_PER_M_USD)
     + (output_tokens / 1_000_000 * MODEL_COST_OUTPUT_PER_M_USD)
```

For Ollama and Groq free tier, `cost_usd = 0.0`.

### Prediction-Level Cost Rollup

After generating a prediction, sum all `model_usage_log` rows for the same `correlation_id` to compute the total cost of the prediction. Store this in `predictions.rationale` as `total_llm_cost_usd`. Surface it in the Admin UI predictions view.

### ops.model_usage_log Schema

```sql
CREATE TABLE ops.model_usage_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id uuid,
    job_run_id uuid REFERENCES ops.job_runs(id),
    agent text NOT NULL,
    model_provider text NOT NULL,
    model_name text NOT NULL,
    call_purpose text NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    context_window_tokens integer NOT NULL,
    context_utilization_pct numeric(5, 2) NOT NULL,
    compression_applied boolean NOT NULL DEFAULT false,
    compression_type text,
    cost_usd numeric(10, 6) NOT NULL DEFAULT 0,
    duration_ms integer,
    output_valid boolean NOT NULL DEFAULT true,
    hallucination_flags jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX model_usage_log_correlation_idx
    ON ops.model_usage_log (correlation_id, created_at DESC);

CREATE INDEX model_usage_log_created_at_idx
    ON ops.model_usage_log (created_at DESC);
```

`call_purpose` values: `normalization`, `entity_extraction`, `sentiment`, `prediction_generation`, `evidence_grounding_check`, `context_compression_summary`, `self_consistency_run`.

---

## Cost Reduction Harness

### 1. Tiered Model Routing

Different pipeline stages have different quality requirements. Use the cheapest model that produces acceptable output for each task.

| Stage | Recommended model | Reason |
|---|---|---|
| Payload structural normalization | Ollama `llama3.2` or Haiku | High volume, simple JSON mapping |
| Entity extraction | Ollama `llama3.2` or Haiku | Repetitive, low complexity |
| Sentiment scoring | Ollama `llama3.2` or Haiku | Single-dimension output |
| Context compression summary | Ollama `llama3.2` or Haiku | Summarization, not reasoning |
| Prediction generation | `qwen2.5:7b` / Groq 70B / Sonnet | Needs structured reasoning + tool use |
| Evidence grounding check | Ollama `llama3.2` or Haiku | Simple grounding comparison |
| Self-consistency run | Same as prediction model | Must match prediction quality |

Configure the cheap model separately:

```
AI_MODEL_PROVIDER_CHEAP=ollama
AI_MODEL_NAME_CHEAP=llama3.2
```

### 2. Prompt Caching (Anthropic)

The Anthropic API supports prefix caching — tokens in a cached prefix cost ~10% of normal input token price on cache hits. Cache the static parts of each agent's system prompt.

Cacheable content (changes infrequently):
- The full system prompt (instructions, output schema, causation rules)
- The list of active prediction targets
- The feature set definition

The `ModelClient` must mark these sections with Anthropic's cache control when the provider is `anthropic`. See the `claude-api` skill for implementation.

Expected cache hit rate in steady-state: 80%–90% for prediction runs (system prompt is re-sent with every call). This reduces effective input token cost by ~70–80%.

### 3. Batching Normalizations

For news/events normalization, batch multiple records into a single LLM call instead of one call per record:

```
NORMALIZATION_BATCH_SIZE=10    # records per LLM call
```

The model receives N records as a JSON array and returns N normalized outputs. This amortizes the system prompt token cost across N records.

Batching constraints:
- Batch size must not push context above `CONTEXT_WARNING_THRESHOLD_PCT`.
- Each item in the batch must be independently validatable (no shared state between items).
- If one item fails validation in a batch, the batch is split and items are retried individually.

### 4. Deduplication Before LLM Calls

Semantically duplicate news articles about the same event waste LLM inference budget. Before passing records to the normalization LLM, deduplicate using a fast heuristic:

1. Hash the first 200 characters of each article title + body.
2. Compute fuzzy similarity (Levenshtein ratio) for title pairs within the same 15-minute window.
3. If similarity > `DEDUP_SIMILARITY_THRESHOLD` (default: 0.85), mark the later record as a duplicate and skip LLM normalization.

```
DEDUP_SIMILARITY_THRESHOLD=0.85
DEDUP_WINDOW_MINUTES=15
```

### 5. Spend Budget per Pipeline Run

Set a maximum allowable spend per prediction batch run. If the running cost total (from `ops.model_usage_log`) approaches the budget, stop generating new predictions and log a WARNING.

```
MAX_SPEND_PER_RUN_USD=0.50    # $0.50 cap per prediction batch
MAX_SPEND_DAILY_USD=5.00      # $5.00 daily cap across all runs
```

For Ollama and Groq free-tier, these caps are informational only (cost = $0) but the token-count tracking still runs to keep context utilization visible.

---

## Admin UI Integration

The Admin UI (see `docs/admin_ui.md`) exposes cost and context metrics in the Dashboard:

- **Model usage today**: total input tokens, output tokens, and cost USD for the current day.
- **Cost per prediction**: average `total_llm_cost_usd` across predictions in the last 7 days.
- **Context utilization**: histogram of `context_utilization_pct` values from the last 24 hours; spikes indicate prompts that need trimming.
- **Compression events**: count of soft and hard compressions in the last 24 hours; sustained high counts signal that `MAX_FEATURES_PER_PREDICTION` should be reduced.
- **Hallucination flags**: count of predictions tagged `hallucination_risk = True` or `probability_extreme_flag = True` pending review.

Additional Pipeline Settings page levers added by this document:

| Setting | Default | Description |
|---|---|---|
| `MODEL_TEMPERATURE` | 0.1 | Sampling temperature for all prediction generation calls |
| `CONTEXT_WARNING_THRESHOLD_PCT` | 0.75 | Start soft compression at this utilization |
| `CONTEXT_CRITICAL_THRESHOLD_PCT` | 0.90 | Hard compress at this utilization |
| `MAX_FEATURES_CRITICAL` | 10 | Feature count kept after hard compression |
| `CONTEXT_KEEP_TOOL_RESULTS` | 5 | Tool call results to keep in full during multi-turn loops |
| `NORMALIZATION_BATCH_SIZE` | 10 | Records per normalization LLM call |
| `DEDUP_SIMILARITY_THRESHOLD` | 0.85 | Fuzzy similarity cutoff for pre-LLM deduplication |
| `MAX_SPEND_PER_RUN_USD` | 0.50 | Spend cap per prediction batch |
| `MAX_SPEND_DAILY_USD` | 5.00 | Daily spend cap across all runs |
| `HALLUCINATION_PROB_LOW` | 0.05 | Flag predictions with probability below this |
| `HALLUCINATION_PROB_HIGH` | 0.95 | Flag predictions with probability above this |
| `SELF_CONSISTENCY_ENABLED` | false | Run prediction N times and compare |
| `SELF_CONSISTENCY_RUNS` | 3 | Runs for consistency check (if enabled) |
| `MAX_OUTPUT_VALIDATION_RETRIES` | 2 | Retry count for malformed model outputs |
