# AI Processing Safety: Loop Prevention, Backoff, and Human-in-the-Loop

## Risk Areas

| Risk | Where it occurs | Consequence |
|---|---|---|
| LLM tool-call loop | Prediction engine calls tools cyclically | Runaway API spend, stalled pipeline |
| Connector retry storm | API connector retries indefinitely on persistent failure | Rate limit bans, wasted compute |
| Orchestrator deadlock | Upstream job never completes, blocking all downstream stages | Pipeline halt |
| Calibration collapse | Model always predicts probability near 0.0 or 1.0 | Useless predictions, false alerts |
| Runaway backtest | Backtest job iterates over unbounded time range | OOM, DB saturation |

---

## Agent-Level Loop Guards

Apply to any LLM agent that makes sequential tool calls (Prediction Engine, Normalization Agent when using LLM).

### Max Tool Calls Per Invocation

Each agent invocation is budgeted a maximum number of tool calls. If the budget is exceeded, the agent stops, logs a `CRITICAL` event, and returns a partial result tagged `loop_budget_exceeded`.

```
MAX_AGENT_TOOL_CALLS=20          # hard limit per single agent invocation
MAX_AGENT_ITERATIONS=5           # max times the agent may re-enter its reasoning loop
```

### Cycle Detection

After each tool call, compare the last call's `(tool_name, input_hash)` against the prior N calls. If a repeating pattern of length 2 or more is detected, stop immediately.

```python
# Pseudocode
def detect_cycle(call_history: list[ToolCall], window: int = 6) -> bool:
    recent = call_history[-window:]
    for pattern_len in range(2, len(recent) // 2 + 1):
        if recent[-pattern_len:] == recent[-2*pattern_len:-pattern_len]:
            return True
    return False
```

### Token Budget

Set a max token budget per agent invocation. If the LLM response approaches the context window limit, truncate and return with a `context_overflow` flag rather than silently hallucinating.

```
MAX_AGENT_INPUT_TOKENS=6000      # input context fed to the model
MAX_AGENT_OUTPUT_TOKENS=2000     # max tokens the model may generate per call
```

---

## Job-Level Loop Guards

Apply to all pipeline stages managed by the Orchestrator via `ops.job_runs`.

### Schema Note

The current `ops.job_runs` table tracks `status` but not `attempt_count`. A migration is needed to add:

```sql
-- Migration: add attempt tracking to job_runs
ALTER TABLE ops.job_runs ADD COLUMN attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE ops.job_runs ADD COLUMN max_attempts integer NOT NULL DEFAULT 3;
```

Until this migration runs, track attempt counts in the existing `metadata jsonb` field.

### Job Timeout

Each job has a maximum wall-clock execution time. If the job does not complete within `JOB_MAX_RUNTIME_SECONDS`, the Orchestrator marks it `failed` and schedules a retry.

```
JOB_MAX_RUNTIME_SECONDS=300      # 5 minutes default; override per job type
INGESTION_JOB_MAX_RUNTIME_SECONDS=120
PREDICTION_JOB_MAX_RUNTIME_SECONDS=180
EVALUATION_JOB_MAX_RUNTIME_SECONDS=60
```

---

## Exponential Backoff Strategy

All retryable failures (connectors, alert deliveries, job failures) follow this three-tier escalation model.

### Backoff Formula

```
delay = min(BASE_DELAY * 2^attempt, MAX_DELAY) + jitter
jitter = random(0, delay * 0.1)

BASE_DELAY = 1 second
MAX_DELAY  = 300 seconds (5 minutes)
```

| Attempt | Delay (before jitter) |
|---|---|
| 1 | 2s |
| 2 | 4s |
| 3 | 8s |
| 4 | 16s |
| 5 | 32s |
| 6 | 64s |
| 7 | 128s |
| 8 | 300s (capped) |

### Three-Tier Escalation

**Tier 1 — Auto-retry (attempts 1–3)**
- Retry automatically with exponential backoff.
- Log each attempt at `WARNING` level with attempt number and error detail.
- No operator notification.

**Tier 2 — Dead-letter + Operator Alert (attempts 4–5)**
- Move job to `dead_letter` status in `ops.job_runs`.
- Send a HITL alert to Telegram (separate chat or labeled `[OPS]`).
- Do not retry automatically. Wait for operator acknowledgement or manual re-queue via Admin UI.

**Tier 3 — Source Pause (attempt 6+)**
- The Orchestrator checks for sources with `attempt_count >= 6` on any job of type `ingestion`.
- Sets `ops.api_sources.is_active = false` for the offending source.
- Logs at `CRITICAL` level.
- Sends a second HITL alert indicating the source has been paused.
- Require manual operator action in the Admin UI (Sources page → Enable) to re-enable.
- Non-source failures (feature generation, prediction, evaluation) escalate to Tier 3 by
  pausing that pipeline stage via a `config_overrides` key, not by disabling a source.

---

## Human-in-the-Loop (HITL) Escalation

### Auto-Escalation Triggers

| Trigger | Threshold | Action |
|---|---|---|
| Connector consecutive failures | attempts 4–5 without success | Tier 2: dead-letter + operator alert |
| Connector consecutive failures | attempt 6+ | Tier 3: source disabled, second HITL alert |
| Connector down duration | > 24 hours with no successful fetch | Tier 3 (fast-track): source disabled regardless of attempt count |
| Prediction probability extremes | > 10 consecutive predictions with probability < 0.05 or > 0.95 | Alert: model likely uncalibrated |
| Evaluation void rate | > 50% void in a rolling 24-hour window | Alert: market data quality issue |
| New model fails baseline | Validation metrics below prior model | Block promotion, alert researcher |
| LLM loop detected | Any `loop_budget_exceeded` or `cycle_detected` event | Alert: agent requires inspection |

### HITL Alert Format

HITL alerts use the same Telegram delivery channel as prediction alerts but include an `[OPS]` label in the message title so they can be routed to a separate chat or filtered.

Payload fields:
- `alert_type`: `ops_escalation`
- `component`: affected agent or source name
- `trigger`: the escalation trigger description
- `consecutive_failures` or `metric_value`
- `job_run_id`: the failing job UUID for direct lookup
- `recommended_action`: human-readable next step

### Operator Actions

| Action | How |
|---|---|
| Resume a dead-letter job | Update `ops.job_runs.status` back to `queued` |
| Disable a failing source | Set `ops.api_sources.is_active = false` |
| Re-enable a source | Set `ops.api_sources.is_active = true` |
| Override a void prediction | Manual insert to `evaluation.evaluation_results` with `evaluation_state = 'evaluated'` |
| Roll back a model version | Update `ops.job_runs` metadata to reference the prior `model_version_id` |

---

## Memory and Context Window Management

### Feature Snapshot Trimming

Feature snapshots can contain dozens of feature keys. Before passing a snapshot to the Prediction Engine LLM, trim to the N most relevant features for the target asset type.

```
MAX_FEATURES_PER_PREDICTION=25   # max feature keys passed to the model
```

Relevance is determined by:
1. Features in the active `feature_set` for the prediction target.
2. Recency — prefer features with `available_at` closest to `issuance_time`.
3. Non-null values only — drop features with null `numeric_value` and null `text_value`.

### Evidence Summary Length Limit

Evidence summaries written by the Prediction Engine must not exceed `MAX_EVIDENCE_SUMMARY_CHARS` characters. If the model generates a longer summary, truncate at the last complete sentence before the limit.

```
MAX_EVIDENCE_SUMMARY_CHARS=1000
```

### Context Carry-Forward

Between pipeline stages, pass only structured identifiers (UUIDs), not raw payloads. Each agent fetches its own data using its skill functions. This prevents context from growing unboundedly across stages.

---

## Configuration Reference

All loop and safety limits are configurable via environment variables (see `.env.example`):

```
MAX_AGENT_TOOL_CALLS
MAX_AGENT_ITERATIONS
MAX_AGENT_INPUT_TOKENS
MAX_AGENT_OUTPUT_TOKENS
JOB_MAX_RUNTIME_SECONDS
MAX_FEATURES_PER_PREDICTION
MAX_EVIDENCE_SUMMARY_CHARS
```
