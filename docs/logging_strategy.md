# Logging Strategy

## Goals

- Every processing event is traceable from raw API input to final prediction and evaluation.
- A single correlation ID threads through all stages of one pipeline run.
- Logs are machine-readable (JSON) for easy ingestion into any log aggregator.
- Log location is consistent and configurable so developers and CI environments use the same paths.

---

## Log Location

| Environment | Log directory | File |
|---|---|---|
| Local dev | `./logs/` (project root) | `app.log` |
| Staging / Production | Configurable via `LOG_FILE_PATH` | `app.log` |
| Docker | stdout + stderr (recommended) | Collected by container runtime |

Default local path: `./logs/app.log`

Logs rotate daily. Rotated files are named `app.YYYY-MM-DD.log`.

```
LOG_FILE_PATH=./logs/app.log
LOG_MAX_BYTES=52428800        # 50 MB per file before rotation
LOG_BACKUP_COUNT=14           # keep 14 rotated files (14 days)
```

For production, prefer stdout/stderr so the container runtime or log forwarder handles routing. Set `LOG_TO_STDOUT=true` to disable file output.

---

## Log Format: JSON

Every log line is a single JSON object. No multi-line stack traces — exceptions are serialized into the `error` field.

### Schema

```json
{
  "timestamp": "2026-04-18T14:32:01.123Z",
  "level": "INFO",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "agent": "prediction_engine",
  "source": "agents.prediction.engine.generate_prediction",
  "message": "prediction created",
  "context": {
    "prediction_id": "uuid",
    "asset_symbol": "BTC",
    "probability": 0.87,
    "horizon_hours": 24
  },
  "error": null,
  "duration_ms": 342
}
```

### Field Definitions

| Field | Type | Required | Description |
|---|---|---|---|
| `timestamp` | ISO 8601 UTC string | Yes | Log event time, always UTC |
| `level` | string | Yes | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `correlation_id` | UUID string | Yes | Shared across all log lines for one pipeline run (see below) |
| `agent` | string | Yes | Name of the agent or service emitting the log |
| `source` | string | Yes | Python module path + function name |
| `message` | string | Yes | Short human-readable description of the event |
| `context` | object | No | Structured key-value pairs relevant to the event |
| `error` | object or null | No | Serialized exception: `{"type": "...", "message": "...", "traceback": "..."}` |
| `duration_ms` | integer or null | No | Wall-clock duration of the operation in milliseconds |

---

## Correlation ID (Thread ID)

The **correlation ID** is a UUID that uniquely identifies a single end-to-end processing flow: from API data ingestion through normalization, feature generation, prediction, evaluation, and alerting.

### How It Works

1. The Orchestrator generates a new `correlation_id` (UUID v4) at the start of each pipeline run.
2. The `correlation_id` is written to `ops.job_runs.correlation_id` (already in schema).
3. Every downstream agent receives the `correlation_id` as part of its job context and includes it in every log line it emits.
4. Every `ops.audit_logs` row includes the `correlation_id` (already in schema).
5. To trace a single prediction end-to-end, filter logs by `correlation_id`.

### Example Trace

```
correlation_id: a1b2c3d4-...

14:30:00  INFO  orchestrator     job queued: ingest_alpha_vantage
14:30:01  INFO  source_connector fetch started: alpha_vantage, since=2026-04-18T02:30:00Z
14:30:02  INFO  source_connector 47 records fetched, 3 duplicates skipped
14:30:03  INFO  normalization    47 events normalized
14:30:04  INFO  feature_engine   feature snapshot created: snap_id=uuid, asset=BTC
14:30:05  INFO  prediction_engine prediction generated: pred_id=uuid, p=0.87
14:30:05  INFO  alerting         alert sent: pred_id=uuid, telegram=ok
```

All lines share the same `correlation_id` — one query retrieves the full trace.

---

## Log Levels

| Level | When to use |
|---|---|
| `DEBUG` | Raw API response bodies, individual feature values, tool call inputs/outputs, retry attempt details |
| `INFO` | Job start/end, record counts ingested, prediction created, evaluation completed, alert sent |
| `WARNING` | Retry attempt (transient failure), skipped duplicate record, stale data detected, low confidence prediction (< 0.3) |
| `ERROR` | Connector failure (after all retries), validation error, DB write failure, malformed model output |
| `CRITICAL` | Loop detected in agent, pipeline paused, HITL escalation triggered, job moved to dead-letter |

In production, set `LOG_LEVEL=INFO`. In local development, `LOG_LEVEL=DEBUG` for full visibility.

---

## LLM Call Log Fields

Every LLM call emits a log line with these additional fields inside `context`:

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
    "call_purpose": "prediction_generation",
    "input_tokens": 2847,
    "output_tokens": 312,
    "context_window_tokens": 200000,
    "context_utilization_pct": 1.4,
    "compression_applied": false,
    "compression_type": null,
    "cost_usd": 0.013251,
    "output_valid": true,
    "hallucination_flags": {}
  },
  "duration_ms": 1240
}
```

`cost_usd` is `0.0` for Ollama and Groq free tier — still logged to keep utilization metrics consistent. Every LLM call is also persisted to `ops.model_usage_log` for cost rollup and Admin UI dashboarding (see `docs/context_and_cost_management.md`).

## What to Always Log

These events must always be logged at the specified level regardless of `LOG_LEVEL`:

| Event | Level | Required context fields |
|---|---|---|
| Job started | INFO | `job_name`, `job_run_id`, `correlation_id` |
| Job completed | INFO | `job_name`, `job_run_id`, `duration_ms`, `records_processed` |
| Job failed | ERROR | `job_name`, `job_run_id`, `attempt_count`, `error` |
| Job moved to dead-letter | CRITICAL | `job_name`, `job_run_id`, `total_attempts`, `error` |
| Prediction created | INFO | `prediction_id`, `asset_symbol`, `probability`, `horizon_hours`, `model_version` |
| Evaluation completed | INFO | `prediction_id`, `evaluation_state`, `brier_score`, `directional_correct` |
| Alert sent | INFO | `prediction_id`, `alert_rule_id`, `delivery_status` |
| Loop detected in agent | CRITICAL | `agent`, `tool_call_count`, `cycle_pattern` |
| HITL escalation triggered | CRITICAL | `trigger`, `component`, `consecutive_failures` |
| Source connector paused | CRITICAL | `source_name`, `reason` |

---

## Retention

| Log content | Retention |
|---|---|
| DEBUG | 7 days |
| INFO and above | 30 days |
| ERROR and CRITICAL | 90 days |
| Audit log (DB) | Indefinite (append-only table) |

---

## Implementation Notes

Use `structlog` (Python) for structured JSON logging. It handles log level filtering, timestamp formatting, and JSON serialization out of the box.

```python
import structlog

log = structlog.get_logger()

log.info(
    "prediction created",
    correlation_id=correlation_id,
    agent="prediction_engine",
    prediction_id=str(prediction.id),
    asset_symbol=asset.symbol,
    probability=float(prediction.probability),
    horizon_hours=target.horizon_hours,
    duration_ms=elapsed_ms,
)
```

Configure `structlog` once at application startup with:
- `JSONRenderer` for all environments
- UTC timestamp processor
- Log level filter matching `LOG_LEVEL` env var
- Exception formatter that serializes tracebacks into the `error` field (not as multi-line text)
