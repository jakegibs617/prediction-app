# Agent Skills and Tool Contracts

This document defines the tool functions (skills) each agent requires. These are the callable units of work each agent depends on — implemented as Python functions and exposed as tools to LLM-based agents via the Claude SDK or equivalent framework.

Each skill should:
- Have a single, testable responsibility
- Be idempotent where possible
- Map directly to a unit test in the test plan

---

## Source Connector Agent

Fetches raw records from external APIs and appends them to `ingestion.raw_source_records`.

| Skill | Signature | Purpose |
|---|---|---|
| `fetch_with_retry` | `(url, headers, params, max_attempts) -> Response` | HTTP GET with exponential backoff on 429 and 5xx responses |
| `check_duplicate` | `(source_name, external_id, version) -> bool` | Query `ingestion.raw_source_records` to detect existing records before insert |
| `write_raw_record` | `(record: RawSourceRecord) -> UUID` | Append-only insert to `ingestion.raw_source_records` |
| `write_versioned_record` | `(record: RawSourceRecord, prior_id: UUID) -> UUID` | Insert a new version of a revised source record, linking to the prior version |
| `log_job_run` | `(source_name, status, error_detail) -> UUID` | Write outcome to `ops.job_runs` |
| `log_audit` | `(entity_type, entity_id, action, metadata) -> void` | Append to `ops.audit_logs` for any prediction-relevant action |

---

## Normalization Agent

Converts raw source payloads into structured `ingestion.normalized_events` rows with extracted signals.

| Skill | Signature | Purpose |
|---|---|---|
| `read_unprocessed_records` | `(source_name, batch_size) -> list[RawSourceRecord]` | Fetch raw records not yet normalized, ordered by ingestion time |
| `extract_entities` | `(text) -> list[Entity]` | NLP: people, organizations, and assets mentioned in text |
| `extract_sentiment` | `(text) -> SentimentResult` | Sentiment score (positive/negative/neutral) with confidence |
| `extract_topics` | `(text) -> list[Topic]` | Topic classification (e.g. earnings, geopolitics, energy, inflation) |
| `extract_geography` | `(text) -> list[GeoTag]` | Country, region, and city tags |
| `score_event_severity` | `(event: NormalizedEvent) -> float` | Severity signal for GDELT and event-coded feeds (0.0–1.0) |
| `write_normalized_event` | `(event: NormalizedEvent) -> UUID` | Append-only insert to `ingestion.normalized_events` |

---

## Feature Engineering Agent

Computes features from normalized events and price data with strict anti-lookahead enforcement. Writes snapshots to `features.*`.

| Skill | Signature | Purpose |
|---|---|---|
| `read_events_before` | `(cutoff_time, filters) -> list[NormalizedEvent]` | Fetch normalized events with `event_at < cutoff_time` — hard lookahead guard |
| `read_prices_before` | `(asset_id, cutoff_time) -> list[PriceBar]` | Fetch price bars with `bar_at < cutoff_time` — hard lookahead guard |
| `compute_rolling_window` | `(values, window_size, cutoff_time) -> WindowResult` | Rolling stats (mean, std, count) using only data available before cutoff |
| `align_macro_release` | `(series, release_calendar) -> list[MacroPoint]` | Join macro values to their public release timestamps, not observation periods |
| `write_feature_snapshot` | `(snapshot: FeatureSnapshot) -> UUID` | Insert into `features.feature_snapshots` |
| `write_feature_values` | `(values: list[FeatureValue]) -> void` | Bulk insert into `features.feature_values` |
| `write_feature_lineage` | `(feature_id, source_record_ids) -> void` | Insert rows into `features.feature_lineage` linking feature to source |

---

## Prediction Engine Agent

Generates probabilistic predictions. This is the LLM-facing agent — all other skills serve as tools it calls.

| Skill | Signature | Purpose |
|---|---|---|
| `read_active_targets` | `() -> list[PredictionTarget]` | Fetch active rows from `predictions.prediction_targets` |
| `read_feature_snapshot` | `(asset_id, issuance_time) -> FeatureSnapshot` | Point-in-time snapshot — returns the snapshot frozen at or before `issuance_time` |
| `validate_no_future_leak` | `(snapshot: FeatureSnapshot, issuance_time) -> ValidationResult` | Assert all feature values have `available_at < issuance_time`; reject if not |
| `label_claim_type` | `(claim_text, evidence_metadata) -> ClaimLabel` | Return `correlation`, `causal_hypothesis`, or `unsupported` — enforces causation guardrails |
| `get_or_create_model_version` | `(model_name, config_hash) -> UUID` | Lookup or insert into `predictions.model_versions` |
| `get_or_create_prompt_version` | `(prompt_text, prompt_hash) -> UUID` | Lookup or insert into `predictions.prompt_versions` |
| `write_prediction` | `(prediction: Prediction) -> UUID` | Immutable insert to `predictions.predictions` — rejects any field updates after creation |
| `write_prediction_status` | `(prediction_id, status, reason) -> void` | Append to `predictions.prediction_status_history` |

---

## Evaluation Engine Agent

Settles predictions after their horizon expires and computes scoring metrics.

| Skill | Signature | Purpose |
|---|---|---|
| `get_evaluable_predictions` | `() -> list[Prediction]` | Fetch predictions past `horizon_end_at` with no existing evaluation result |
| `get_settlement_price` | `(asset_id, settlement_time) -> PriceBar` | Lookup in `market_data.price_bars` at or nearest to settlement time |
| `get_trading_day_end` | `(asset_id, target_date) -> datetime` | Calendar-aware equity settlement — skips weekends and market holidays |
| `compute_directional_accuracy` | `(predicted_direction, actual_return) -> bool` | Binary correct/incorrect based on predicted direction and realized return |
| `compute_brier_score` | `(probability, outcome) -> float` | `(probability - outcome)^2` where outcome is 0 or 1 |
| `compute_calibration_bucket` | `(probability) -> str` | Assign to 0.1-width bucket (e.g. `"0.8-0.9"`) |
| `compute_paper_return` | `(prediction, outcome, cost_bps) -> float` | Simulated P&L with explicit cost and slippage assumptions |
| `write_evaluation_result` | `(result: EvaluationResult) -> UUID` | Append to `evaluation.evaluation_results` |
| `mark_prediction_void` | `(prediction_id, reason) -> void` | Update status to `void_missing_data` when settlement price is unavailable |

---

## Alerting Agent

Sends high-confidence predictions to Telegram via bot message delivery.

| Skill | Signature | Purpose |
|---|---|---|
| `get_alertable_predictions` | `(min_probability, max_horizon_hours) -> list[Prediction]` | Filter live (non-backtest) predictions by configured thresholds |
| `check_already_alerted` | `(prediction_id, alert_rule_id) -> bool` | Idempotency check against `ops.alert_deliveries` — prevents duplicate messages |
| `format_alert_payload` | `(prediction: Prediction) -> dict` | Build the Telegram alert payload with all required fields |
| `send_telegram_message` | `(chat_id, payload, max_attempts) -> DeliveryResult` | Telegram Bot API send with bounded retry; returns success or final failure status |
| `write_alert_delivery` | `(delivery: AlertDelivery) -> UUID` | Log attempt outcome to `ops.alert_deliveries` regardless of success/failure |

---

## Orchestrator

Schedules and coordinates all pipeline stages using the `ops.job_runs` table as a job queue.

| Skill | Signature | Purpose |
|---|---|---|
| `get_pending_jobs` | `(job_type, due_before) -> list[JobRun]` | Poll `ops.job_runs` for jobs that are due and not yet claimed |
| `acquire_job_lock` | `(job_id, worker_id) -> bool` | Atomic lock via DB update — prevents duplicate execution across workers |
| `release_job_lock` | `(job_id, status, error_detail) -> void` | Mark job complete, failed, or retryable; increment attempt counter |
| `send_to_dead_letter` | `(job_id, reason) -> void` | Move job to dead-letter state after max retry attempts exceeded |
| `emit_correlation_id` | `() -> str` | Generate a UUID correlation ID to propagate through all stages of one pipeline run |
| `check_upstream_complete` | `(upstream_job_type, run_id) -> bool` | Dependency gate — verify prerequisite stage succeeded before triggering downstream |

---

## Context and Cost Management (Applied to All LLM Agents)

These skills wrap every LLM call. They are not exposed to the LLM as tools — they execute in the agent harness layer before and after each model invocation.

| Skill | Signature | Purpose |
|---|---|---|
| `estimate_token_count` | `(text: str) -> int` | Fast heuristic token count (4 chars ≈ 1 token); used pre-call without a tokenizer dependency |
| `check_context_utilization` | `(estimated_tokens, context_window) -> UtilizationLevel` | Return `normal`, `warning`, or `critical` based on configured thresholds |
| `soft_compress_features` | `(snapshot: FeatureSnapshot, target_tokens: int) -> FeatureSnapshot` | Sort by relevance score, drop lowest-scoring features until under target; log omitted keys |
| `hard_compress_context` | `(context: AgentContext, cheap_model: ModelClient) -> AgentContext` | Summarize full evidence block to 3 sentences; keep only top `MAX_FEATURES_CRITICAL` features |
| `slide_tool_window` | `(history: list[ToolCall], keep_n: int) -> list[ToolCall]` | Replace oldest tool results with one-line summaries; keep last `CONTEXT_KEEP_TOOL_RESULTS` in full |
| `log_model_usage` | `(call_meta: ModelCallMeta) -> UUID` | Write to `ops.model_usage_log` and emit structured log line with token counts, cost, and utilization |
| `compute_call_cost` | `(input_tokens, output_tokens, provider, model) -> float` | Calculate `cost_usd` from env-configured per-token rates |
| `check_spend_budget` | `(correlation_id, max_run_usd, max_daily_usd) -> bool` | Sum `ops.model_usage_log` for current run and day; return `False` if either cap is exceeded |
| `validate_model_output` | `(raw_output: str, schema: type[BaseModel], max_retries: int) -> BaseModel` | Parse and validate model output; retry with corrective prompt on failure |
| `check_evidence_grounding` | `(evidence_summary: str, snapshot: FeatureSnapshot) -> GroundingResult` | Flag numeric values in evidence that don't appear in the snapshot |
| `check_probability_sanity` | `(probability: float, low: float, high: float) -> bool` | Flag predictions outside `[HALLUCINATION_PROB_LOW, HALLUCINATION_PROB_HIGH]` |

## Validation Agent (Runs Before Any Record Is Written)

The `ValidatorPipeline` runs synchronously within the Source Connector Agent, between fetching and writing to the database. No record bypasses it.

| Skill | Signature | Purpose |
|---|---|---|
| `check_payload_size` | `(payload_bytes: bytes, max_bytes: int) -> ValidationResult` | Reject payloads exceeding `MAX_PAYLOAD_BYTES` |
| `check_encoding` | `(payload_bytes: bytes) -> ValidationResult` | Verify UTF-8; strip null bytes; reject on non-decodable content |
| `validate_payload_schema` | `(raw: dict, schema: type[BaseModel]) -> ValidationResult` | Instantiate connector-specific Pydantic schema; collect all field errors |
| `check_value_ranges` | `(record: RawSourceRecord, source_category: str) -> ValidationResult` | Enforce domain range rules: prices > 0, sentiments in [-1,1], timestamps not in far future |
| `check_temporal_sanity` | `(record: RawSourceRecord, max_age_days: int) -> ValidationResult` | Reject records older than `max_age_days`; ensure `released_at <= ingested_at` |
| `check_price_anomaly` | `(record: RawSourceRecord, prior_close: float) -> ValidationWarning` | Flag (not reject) single-period price moves > `MAX_SINGLE_PERIOD_CHANGE_PCT` |
| `compute_checksum` | `(raw_payload: dict) -> str` | SHA-256 of raw payload for deduplication |
| `write_quarantined_record` | `(record: RawSourceRecord, errors: list) -> UUID` | Write record with `validation_status = quarantined` and `validation_errors` JSON |
| `sanitize_for_prompt` | `(text: str, max_chars: int) -> str` | Strip prompt injection patterns; truncate to safe length before LLM inclusion |
| `validate_outbound_url` | `(url: str) -> ValidationResult` | SSRF check: HTTPS required, private IP ranges blocked, length limit |

## Loop Guard (Applied to All LLM Agents)

These skills are injected into every LLM-based agent invocation. They are not called by the LLM directly — they wrap the agent execution loop.

| Skill | Signature | Purpose |
|---|---|---|
| `check_tool_budget` | `(call_count, max_calls) -> bool` | Return `False` if the agent has exceeded `MAX_AGENT_TOOL_CALLS`; triggers graceful stop |
| `detect_cycle` | `(call_history: list[ToolCall], window) -> bool` | Check last N calls for a repeating `(tool_name, input_hash)` pattern |
| `enforce_context_limit` | `(prompt_tokens, max_tokens) -> str` | Trim feature snapshot or evidence context to fit within `MAX_AGENT_INPUT_TOKENS` |
| `truncate_evidence` | `(text, max_chars) -> str` | Truncate evidence summary at last complete sentence within `MAX_EVIDENCE_SUMMARY_CHARS` |

## In-Memory Cache

A lightweight TTL cache shared within a single worker process. Reduces DB round-trips for rarely changing reference data.

| Skill | TTL | Cached Data |
|---|---|---|
| `get_cached_assets` | 300s | `market_data.assets` rows |
| `get_cached_alert_rules` | 60s | `ops.alert_rules` active rows |
| `get_cached_prediction_targets` | 300s | `predictions.prediction_targets` active rows |
| `get_cached_model_version` | 300s | `predictions.model_versions` lookup by name+version |
| `invalidate_cache` | — | Flush all entries; called on worker startup and after DB writes to cached tables |

Cache entries are invalidated:
1. On TTL expiry.
2. On worker startup (always reload from DB).
3. Explicitly after any write to the underlying table.

## Discovery Agent (Cron — Every 12 Hours)

Runs on a 12-hour schedule. Does not generate predictions — checks the health and schema of all registered API sources and surfaces changes for operator review.

| Skill | Signature | Purpose |
|---|---|---|
| `get_active_sources` | `() -> list[ApiSource]` | Fetch all active rows from `ops.api_sources` |
| `probe_source_health` | `(source: ApiSource) -> HealthResult` | Make a minimal test request to each source's API and record response time and status |
| `detect_schema_change` | `(source_name, current_sample) -> ChangeReport` | Compare a sample raw payload against the last known payload shape; flag new or removed fields |
| `discover_new_assets` | `(source_name) -> list[Asset]` | Check if the source has added new symbols or series not yet in `market_data.assets` |
| `write_discovery_log` | `(report: DiscoveryReport) -> void` | Write findings to `ops.audit_logs` with `action = 'discovery_run'` |
| `send_discovery_summary` | `(report: DiscoveryReport) -> void` | If any source is unhealthy or schema changes are detected, send a summary via Telegram HITL alert |

## Shared / Cross-Agent Utilities

These are not agent-specific — they are shared helpers used across multiple agents.

| Skill | Signature | Purpose |
|---|---|---|
| `get_utc_now` | `() -> datetime` | Canonical UTC timestamp source — never use `datetime.now()` directly |
| `get_asset_by_symbol` | `(symbol, asset_type) -> Asset` | Lookup in `market_data.assets` |
| `read_alert_rules` | `() -> list[AlertRule]` | Fetch active rows from `ops.alert_rules` |
| `structured_log` | `(level, message, correlation_id, context) -> void` | Emit a structured JSON log line with correlation ID for pipeline tracing |

---

## Agent Communication Pattern

All agents communicate via **Postgres job queue** using the `ops.job_runs` table:

1. The Orchestrator inserts a job row for each pipeline stage.
2. Each agent polls for jobs of its type, acquires a lock, and processes.
3. On completion the agent updates the job row with status, metrics, and any error detail.
4. The Orchestrator checks upstream completion before inserting dependent downstream jobs.

This approach requires no additional infrastructure beyond Postgres (already required) and is upgradeable to Celery or Temporal later without changing the skill contracts.

---

## Connector Interface

All source connectors must implement this base contract:

```python
class BaseConnector:
    source_name: str
    base_url: str
    rate_limit_per_minute: int

    def fetch(self, params: dict, since: datetime) -> list[dict]:
        """Fetch raw records from the source API."""
        ...

    def normalize(self, raw: dict) -> RawSourceRecord:
        """Map a raw API response to the canonical RawSourceRecord schema."""
        ...

    def get_external_id(self, raw: dict) -> str:
        """Extract the source's stable identifier for deduplication."""
        ...
```

Connectors are registered in a source registry keyed by `source_name`, which maps to the `ops.api_sources` table.
