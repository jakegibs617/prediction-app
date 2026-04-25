# Prediction App Project Checklist

## Foundation

- [ ] Confirm MVP scope is research and paper-trading only.
- [ ] Review and approve acceptance criteria.
- [ ] Review and approve initial Postgres schema.
- [ ] Choose app stack for API, workers, and UI.
  - Recommendation: Python + FastAPI (API server) + asyncio workers + asyncpg (DB client) + Pydantic (validation).
- [ ] Choose orchestration approach for scheduled and event-driven jobs.
  - Recommendation: APScheduler + `ops.job_runs` Postgres job queue for MVP. Interface is abstracted so it can be replaced with Celery or Temporal later.
- [ ] Define local, staging, and production environments.
- [ ] Create `docker-compose.yml` with a Postgres service for local development.
- [ ] Create `.env` from `.env.example` and fill in real values for all required secrets.
- [ ] Configure a migration runner (Alembic recommended) pointing to the `sql/` directory.
- [ ] Add `.gitignore` covering `.env`, virtual environments, `__pycache__`, and build artifacts.
- [ ] Define and document the agent communication pattern before writing any agent code.
  - Recommendation: Postgres job queue via `ops.job_runs` — no additional infrastructure needed.

## Data Sources

- [ ] Finalize initial source list for global events.
  - Recommendation: GDELT DOC 2.0 (free, no key required).
- [ ] Finalize initial source list for news headlines.
  - Recommendation: NewsAPI (simplest integration; free developer tier; see `.env.example` for key setup).
- [ ] Finalize initial source list for macroeconomic indicators.
  - Recommendation: FRED (free, widely used; see `.env.example` for key setup).
- [ ] Finalize initial source list for equity and crypto price data.
  - Recommendation: Alpha Vantage (single key covers equities and crypto; see `.env.example` for key setup).
- [ ] Document rate limits, auth needs, freshness expectations, and legal usage notes for each source.
- [ ] Define source ownership and failure-handling expectations.

## Database

- [ ] Create PostgreSQL database `experimental_prediction_app`.
- [ ] Run initial SQL bootstrap file.
- [ ] Enable required PostgreSQL extensions in target environments.
- [ ] Establish migration workflow.
- [ ] Set up backup and restore process.
- [ ] Define retention policy for raw payloads, features, alerts, and experiment logs.

## Agent Design

- [ ] Review and approve agent skills and tool contracts (`docs/agent_skills.md`).
- [ ] Review and approve AI safety and loop prevention plan (`docs/ai_safety_and_loops.md`).
- [ ] Review and approve I/O specification (`docs/io_specification.md`).
- [ ] Review and approve logging strategy (`docs/logging_strategy.md`).
- [ ] Review and approve model configuration strategy (`docs/model_configuration.md`).
- [ ] Review and approve Admin UI plan (`docs/admin_ui.md`).
- [ ] Review and approve input validation and security plan (`docs/input_validation_and_security.md`).
- [ ] Implement `BaseConnector` interface that all source connectors must extend.
- [ ] Implement `ModelClient` interface supporting Ollama (default), Groq, Anthropic, and OpenAI.
- [ ] Implement shared utilities (`get_utc_now`, `structured_log`, `log_audit`, `get_asset_by_symbol`).
- [ ] Implement `ops.job_runs` job queue helpers (`acquire_job_lock`, `release_job_lock`, `send_to_dead_letter`).
- [ ] Implement agent loop guard (`detect_cycle`, tool call budget enforcement, max iterations).
- [ ] Configure `structlog` for JSON output with correlation ID propagation.
- [ ] Create `./logs/` directory and configure log rotation.

## Context, Cost, and Hallucination Management

- [ ] Review and approve context and cost management plan (`docs/context_and_cost_management.md`).
- [ ] Implement `estimate_token_count` heuristic counter (no tokenizer dependency).
- [ ] Implement `check_context_utilization` with warning (75%) and critical (90%) thresholds.
- [ ] Implement `soft_compress_features` with relevance scoring and omission logging.
- [ ] Implement `hard_compress_context` using cheap model summarization pass.
- [ ] Implement `slide_tool_window` for multi-turn agent context pruning.
- [ ] Implement `log_model_usage` — write to `ops.model_usage_log` and emit structured log line.
- [ ] Implement `compute_call_cost` using env-configured per-token rates.
- [ ] Implement `check_spend_budget` with per-run and daily caps.
- [ ] Implement `validate_model_output` with retry-on-malformed-output logic.
- [ ] Implement `check_evidence_grounding` to flag hallucinated numeric claims.
- [ ] Implement `check_probability_sanity` to flag extreme probabilities.
- [ ] Configure tiered model routing: cheap model for normalization/extraction, reasoning model for prediction.
- [ ] Enable Anthropic prompt caching for system prompt and prediction target prefix.
- [ ] Implement normalization batching (`NORMALIZATION_BATCH_SIZE=10`).
- [ ] Implement pre-LLM fuzzy deduplication for news/events.
- [ ] Add context and cost metrics to Admin UI Dashboard (utilization histogram, cost per prediction, compression event count, hallucination flag count).
- [ ] Set `MODEL_TEMPERATURE=0.1` as default for all prediction generation calls.

## Schema Migrations Needed Before Implementation

All migrations are in `sql/002_pending_migrations.sql`. Apply this file after `001_init_experimental_prediction_app.sql`.

- [ ] Run `sql/002_pending_migrations.sql` against local dev database.
- [ ] Verify all new indexes and check constraints are present after migration.

Key migrations included:
- `ops.job_runs`: `attempt_count`, `max_attempts`, sanity check constraints
- `ingestion.raw_source_records`: `prior_record_id` (version chain), `validation_status`, `validation_errors`, `validation_warnings`
- `features.feature_values`: `available_at` timestamptz **(critical — anti-lookahead enforcement)**
- `ops.api_sources`: `trust_level`, `rate_limit_per_minute`, `max_record_age_days`, `max_attempts`, `ingest_cron_interval_seconds`, `notes`, `updated_at`; plus check constraints on `auth_type` and `category`
- `predictions.predictions`: `hallucination_risk`, `probability_extreme_flag`, `context_compressed`, `backtest_run_id`; drop auto-default on `correlation_id` so it is set to the pipeline run's correlation_id
- `predictions.prediction_targets`: `updated_at`; check constraint on `asset_type`
- `ops.alert_rules`: `updated_at`
- New table: `ops.config_overrides` (pre-seeded with all configurable keys and defaults)
- New table: `ops.model_usage_log`
- Missing indexes on: `normalized_events`, `feature_lineage`, `feature_snapshots`, `prediction_targets`, `alert_rules`, `job_runs`, `assets`
- Missing check constraints on: `market_data.assets.asset_type`, `normalized_events.event_type`, `normalized_events.sentiment_score`, `normalized_events.severity_score`, `prediction_targets.asset_type`, `alert_deliveries.attempt_count`

**Additional migrations to plan (not yet in 002):**
- [ ] Change `prediction_targets.direction_rule` and `settlement_rule` column type from `text` to `jsonb` once grammar is approved (see `docs/io_specification.md`).
- [ ] Add `ops.discovery_snapshots` table to persist last-known API payload shape for schema-change detection by the Discovery Agent.
- [ ] Add `actor_id` to `ops.audit_logs` (currently `actor_type` text only, no identity column for operator username).

## Memory and Caching

- [ ] Implement in-memory cache with TTL for: asset lookup, alert rules, prediction targets, model/prompt versions.
- [ ] Implement cache invalidation on worker startup (always reload from DB on start).
- [ ] Implement feature snapshot trimming (`MAX_FEATURES_PER_PREDICTION`) before passing to LLM.
- [ ] Implement evidence summary length enforcement (`MAX_EVIDENCE_SUMMARY_CHARS`).

## Cron and Discovery

- [ ] Implement discovery cron (every 12 hours): API health check, schema change detection, new asset discovery.
- [ ] Implement price ingestion cron (every 15 minutes).
- [ ] Implement news/events ingestion cron (every 1 hour).
- [ ] Implement macro data check cron (every 6 hours).
- [ ] Implement evaluation cron (every 24 hours for expired predictions).
- [ ] Implement alert check cron (every 1 hour for alertable predictions).

## Ingestion

- [ ] Build source registry and connector interface.
- [ ] Build connector for first global event source.
- [ ] Build connector for first news source.
- [ ] Build connector for first macro source.
- [ ] Build connector for first market data source.
- [ ] Implement deduplication and versioning for revised records.
- [ ] Implement retry, backoff, and failure logging.

## Normalization and Feature Engineering

- [ ] Define normalized event schema.
- [ ] Build normalization pipeline for raw source records.
- [ ] Define first feature set version.
- [ ] Implement point-in-time feature snapshots.
- [ ] Implement feature lineage tracking.
- [ ] Validate release-time-aware joins for macro data.

## Predictions

- [ ] Define 3 to 5 concrete prediction target types and seed them into `predictions.prediction_targets`.
  - Suggested starting targets:
    - `BTC/USD up > 2% within 24 hours`
    - `ETH/USD down > 3% within 48 hours`
    - `SPY daily return positive` (next trading day close vs. open)
    - `WTI crude oil up > 1% within 24 hours`
- [ ] Implement baseline heuristic prediction engine.
- [ ] Store immutable prediction records.
- [ ] Version model configuration and prompts.
- [ ] Prevent prediction creation from future-leaking features.

## Evaluation

- [ ] Implement target settlement logic for equities.
- [ ] Implement target settlement logic for crypto.
- [ ] Implement Brier score, directional accuracy, and calibration metrics.
- [ ] Add paper-trading return metrics with explicit cost assumptions.
- [ ] Separate live prediction reporting from backtest reporting.

## Alerting

- [ ] Add configurable alert threshold and horizon rules.
- [ ] Implement Telegram alert integration.
- [ ] Ensure alerts only fire for live predictions.
- [ ] Make alert delivery idempotent.
- [ ] Log alert attempts, success, and failure states.
- [ ] Add retry handling for transient Telegram delivery failures.

## Orchestration and Ops

- [ ] Implement job orchestration for ingestion, normalization, features, prediction, evaluation, and alerting.
- [ ] Add structured logging with correlation ids.
- [ ] Add dead-letter handling for repeatedly failing jobs.
- [ ] Add dashboards for job freshness, failures, and evaluation lag.
- [ ] Add audit logging for prediction issuance and evaluation.

## Testing

- [ ] Approve dedicated unit test plan.
- [ ] Implement connector unit tests.
- [ ] Implement feature engineering unit tests.
- [ ] Implement prediction logic unit tests.
- [ ] Implement evaluation unit tests.
- [ ] Implement alerting unit tests.
- [ ] Implement orchestration unit tests.
- [ ] Implement end-to-end integration smoke test.

## Governance and Research Controls

- [ ] Add correlation vs causation labeling rules.
- [ ] Add baseline comparison requirement before promoting new models.
- [ ] Add drift monitoring plan.
- [ ] Add data catalog for all connected sources.
- [ ] Keep a paper-trading-only boundary until validation thresholds are met.

## Admin UI

- [ ] Implement FastAPI `/admin` route prefix with Jinja2 + HTMX templates.
- [ ] Implement HTTP Basic Auth for local dev; session auth for staging/production.
- [ ] Implement role scoping: `viewer`, `operator`, `admin`.
- [ ] Implement Dashboard page (pipeline health, source freshness, prediction summary, dead-letter count).
- [ ] Implement Sources page (CRUD on `ops.api_sources`, enable/disable toggle, trust level management, run-now trigger).
- [ ] Implement Pipeline Settings page (all levers from `docs/admin_ui.md` backed by `ops.config_overrides`).
- [ ] Implement Model Settings page (provider, model name, token limits, Test Connection button).
- [ ] Implement Alert Rules page (thresholds, Telegram destination, Send Test Alert button).
- [ ] Implement Prediction Targets page (view/add/deactivate targets).
- [ ] Implement Dead Letter Queue page (view and requeue/dismiss stuck jobs).
- [ ] Implement Audit Log page (filterable view of `ops.audit_logs`).
- [ ] Implement `ops.config_overrides` resolution: DB override > `.env` > coded default.
- [ ] Ensure all admin actions write to `ops.audit_logs` with before/after values.
- [ ] Mask all secrets in UI after save (API keys and bot tokens show only last 4 chars).

## Input Validation and Security

- [ ] Implement `ValidatorPipeline` with all 7 steps (size, encoding, structural, range, temporal, anomaly, duplicate).
- [ ] Define per-connector `RawPayloadSchema(BaseModel)` Pydantic models for structural validation.
- [ ] Implement `validate_outbound_url()` for SSRF prevention on all operator-supplied URLs.
- [ ] Implement `sanitize_for_prompt(text)` for prompt injection defense.
- [ ] Add structural separation wrapper to all LLM prompts that include external data.
- [ ] Validate all LLM outputs against `PredictionOutput` Pydantic model before acting on them.
- [ ] Configure Jinja2 `autoescape=True` globally across all admin templates.
- [ ] Add `sensitive_keys` masking to `structured_log` utility.
- [ ] Add CI lint rule to flag raw SQL string interpolation (bandit or custom AST check).
- [ ] Enforce `trust_level` gate at normalization entry: only `verified` source records proceed.
- [ ] Document per-source `max_record_age_days` for all connected sources.

## Milestones

- [ ] Milestone 1: Database and connector skeleton complete.
- [ ] Milestone 2: One end-to-end source-to-prediction pipeline complete.
- [ ] Milestone 3: Automatic evaluation and calibration reporting complete.
- [ ] Milestone 4: Telegram alerting complete.
- [ ] Milestone 5: Backtesting and experiment tracking complete.
