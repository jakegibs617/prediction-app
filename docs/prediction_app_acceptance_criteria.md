# Prediction Application: Acceptance Criteria and Test Plan

## Product Goal

Build an agentic orchestration application that ingests public data about global events, news, macroeconomic indicators, and market prices, then produces timestamped probabilistic predictions about short-term stock and crypto moves before outcomes are known.

The system must validate predictions after the forecast horizon expires, score calibration and directional accuracy, and clearly separate:

- predictive correlation
- plausible causal hypotheses
- unsupported causal claims

## Core Product Principles

- Every prediction is immutable once published.
- Every prediction must include a probability, forecast horizon, supporting evidence, and target asset.
- Every prediction must be evaluated automatically after the horizon ends.
- The system must distinguish backtests from live forward predictions.
- The system must not claim causation from observational data alone.
- The system must preserve point-in-time data to avoid lookahead bias.

## Phase 1 Scope

Phase 1 should focus on research and evaluation, not auto-trading.

Included:

- ingesting public data feeds
- generating prediction candidates
- human-readable prediction records
- automatic validation and scoring
- dashboards for prediction history and model calibration
- feature lineage and experiment tracking

Excluded:

- brokerage execution
- autonomous order placement
- leverage or margin recommendations
- portfolio optimization beyond paper portfolios

## User Stories

- As an operator, I can connect public data feeds for news, event, macro, and price data.
- As a researcher, I can define forecast targets such as "BTC up more than 2% in 24h" or "XOM outperforms SPY over 3 trading days."
- As a model agent, I can generate a prediction with evidence and confidence.
- As an evaluator, I can score whether the prediction was correct after the forecast window closes.
- As a researcher, I can inspect calibration, precision, recall, Brier score, and profit proxy metrics.
- As an analyst, I can compare models, prompts, and feature sets using the same frozen historical data.
- As a reviewer, I can see which inputs were available at prediction time.
- As a user, I can see when the system is making a correlation-based claim versus a causal hypothesis.

## Functional Acceptance Criteria

### 1. Data Ingestion

- The system can ingest at least one source for each category:
  - global events
  - news headlines
  - macroeconomic indicators
  - market prices
- Each ingested record stores:
  - source name
  - source event timestamp
  - ingestion timestamp
  - external identifier
  - raw payload
  - normalized payload
- The system retries transient API failures and records failure reasons.
- Duplicate events from the same source are detected and not double-counted.
- Late-arriving or revised data is versioned instead of overwriting prior values silently.

### 2. Feature and Signal Generation

- The system can convert raw records into normalized features with explicit timestamps.
- Every feature includes lineage back to source records.
- Features that use rolling windows are computed only from data available before the forecast issuance time.
- Sentiment, topic, geography, entity extraction, and event severity features are supported for text/event feeds.
- Macro features support lag-aware joins so releases are aligned to their actual public release time, not the observation period alone.

### 3. Prediction Creation

- A prediction record must include:
  - prediction id
  - created at timestamp
  - model or agent version
  - target asset
  - target metric
  - forecast horizon
  - predicted direction or outcome
  - probability
  - evidence summary
  - feature snapshot reference
- Probability must be constrained to 0 through 1.
- Forecast target definitions must be machine-readable and reproducible.
- Once created, predictions are append-only and cannot be edited in place.

### 4. Evaluation and Scoring

- Every prediction records `created_at` (issuance timestamp) and `horizon_end_at` (settlement deadline) in UTC at write time.
- Predictions are evaluated automatically when the target horizon expires.
- The evaluator uses the correct market calendar and trading session logic for each asset type.
- The system computes at minimum:
  - directional accuracy
  - Brier score
  - calibration buckets
  - precision and recall for thresholded signals
  - average return after costs assumptions for paper strategies
- The system distinguishes "not yet evaluable", "evaluated", "void due to missing data", and "superseded target definition" states.
- Only live (non-backtest) predictions that are at least 3 days old are included in accuracy reporting, giving markets sufficient time to resolve before scoring.

### 4a. Accuracy Reporting

- After each evaluation run that settles one or more predictions, the system sends a rolling accuracy summary to Telegram.
- The accuracy report covers live predictions issued within the last 30 days that are at least 3 days old.
- The accuracy report includes at minimum:
  - total number of evaluated predictions in the window
  - overall directional accuracy (correct / total, as a percentage)
  - mean Brier score (0.25 is random baseline; lower is better)
  - per-target breakdown showing count, directional accuracy, and mean Brier score
- The report is not sent if no qualifying evaluated predictions exist for the window (no empty messages).
- Accuracy reports are sent only after new evaluations complete, not on every pipeline tick, to avoid duplicate or stale messages.

### 5. Alerting and Notifications

- If a live prediction has probability `>= 0.85` and a forecast horizon of 3 days or less, the system sends an alert to Telegram.
- The alert is sent only for live forward predictions, not historical backtests.
- The alert payload includes at minimum:
  - prediction id
  - created at timestamp
  - target asset
  - target metric
  - forecast horizon
  - predicted direction or outcome
  - probability
  - evidence summary
- Alerts are idempotent, so retries do not create duplicate Telegram messages for the same prediction unless explicitly configured.
- Failed alert deliveries are retried with backoff and recorded in an alert delivery log.
- The system supports configurable thresholds so the default `0.85` can be changed later without code changes.
- The Telegram bot token and destination chat ID are stored securely outside source control.

### 6. Experimentation and Backtesting

- Historical backtests must run with point-in-time data only.
- The platform prevents lookahead bias by freezing features at forecast issuance time.
- Backtest runs store:
  - code version
  - model version
  - feature set version
  - training window
  - test window
  - target definition
  - metrics
- The UI or reporting layer clearly labels backtest results separately from live predictions.

### 7. Correlation vs Causation Guardrails

- The system may report statistical associations, but causal claims must be labeled as hypotheses unless backed by a stronger identification method.
- Any causal explanation shown to users must cite the evidence type used, such as:
  - temporal precedence only
  - event study
  - natural experiment
  - difference-in-differences
  - instrumental variable
- If only observational correlation is available, the app must explicitly say that causation is unproven.

### 8. Agentic Orchestration

- Separate agents or services exist for:
  - ingestion
  - normalization
  - feature generation
  - prediction generation
  - evaluation
  - reporting
- Each agent has a clear contract, idempotent input handling, and structured outputs.
- Orchestration retries safe tasks and avoids duplicating completed work.
- Failures in one pipeline stage do not corrupt already published predictions.

### 9. Auditability and Governance

- Every prediction and evaluation result is traceable to raw source inputs.
- The system keeps an append-only audit log for prediction issuance and evaluation.
- The system can reproduce why a prediction was made using stored features, prompts, and model metadata.
- Secrets for external APIs are not stored in source control.

## Non-Functional Acceptance Criteria

- Ingestion jobs recover gracefully from temporary source outages.
- The system supports replaying historical events for backtesting.
- Core prediction and evaluation jobs are idempotent.
- The schema supports adding new sources without breaking existing consumers.
- Observability is included for pipeline latency, source freshness, error rate, and evaluation lag.
- All services use structured logging with correlation ids for tracing a prediction across ingestion, feature generation, alerting, and evaluation.
- Database schema changes are managed through versioned migrations.
- Time handling is standardized in UTC in storage, with explicit market timezone conversion only at presentation or settlement boundaries.
- Personally identifiable information is avoided unless there is a clear business need.
- Retention policies are defined for raw payloads, normalized records, feature snapshots, alerts, and experiment logs.
- Secrets are stored in a secrets manager or encrypted environment configuration, never in code or plaintext config files.
- The system defines recovery objectives for pipeline failures and database restoration.
- Production changes require environment separation at minimum for local, staging, and production.
- Access to prediction issuance, model configuration, and alert configuration is role-scoped and auditable.

## Suggested Initial Architecture

- `source-connectors`: fetch and normalize external APIs
- `postgres`: primary system of record for raw records, normalized events, predictions, evaluations, and audit logs
- `event-store`: append-only raw and normalized records backed by Postgres tables and partitioning
- `feature-store`: point-in-time feature views
- `prediction-engine`: heuristic, ML, or LLM-based forecaster
- `evaluation-engine`: target settlement and scoring
- `orchestrator`: schedules jobs and manages dependencies
- `research-ui`: prediction ledger, metrics, and drill-down analysis

## Database Decision

Use PostgreSQL as the primary transactional database from the start.

Why Postgres fits this app well:

- strong support for append-only event and audit tables
- transactional integrity for prediction issuance and evaluation state changes
- flexible JSONB storage for raw API payloads and normalized enrichment metadata
- mature indexing and partitioning options for time-series-heavy workloads
- good compatibility with orchestration tools, analytics tooling, and job queues
- easy path to adding replicas, backups, and warehouse exports later

Initial Postgres responsibilities:

- store raw source payloads with source and ingestion timestamps
- store normalized event records
- store feature lineage metadata and frozen feature snapshots
- store predictions, evaluations, and alert delivery logs
- store experiment metadata, model versions, and prompt versions
- store audit logs and operational job state

Suggested schema groups:

- `ingestion`
- `market_data`
- `features`
- `predictions`
- `evaluation`
- `ops`

Recommended Postgres best practices:

- use UUID primary keys for externally referenced records
- partition high-volume append-only tables by time
- use JSONB for raw payload capture, but keep core query fields relational
- enforce unique constraints on source plus external id plus version where appropriate
- use immutable prediction rows plus related status and evaluation tables instead of in-place mutation
- create explicit indexes for common query paths such as asset, created_at, horizon_end_at, and evaluation_state
- enable automated backups and test restore procedures
- keep migrations forward-only in shared environments

## App Stack Decision

Use Python as the primary application language with FastAPI for the API server and asyncio-based workers.

Why this stack fits this app well:

- Python is the dominant language in the data science, ML, and financial analytics ecosystem; all major connectors, NLP libraries, and ML frameworks have first-class Python support
- FastAPI provides async request handling, automatic OpenAPI docs, and native Pydantic integration for schema validation
- asyncpg gives high-performance async Postgres access without the overhead of a full ORM
- Pydantic enforces typed, validated data models at every agent boundary, catching schema violations before they reach the database
- The async worker pattern maps naturally to the pipeline stages — each stage is an async task that reads from the job queue, processes, and writes results

Initial stack components:

- `fastapi` — API server for research UI and operator endpoints
- `asyncpg` — async Postgres driver
- `pydantic` — domain model validation and schema contracts
- `httpx` — async HTTP client for all external API connectors
- `apscheduler` — job scheduler for orchestration polling loops
- `alembic` — database schema migration management
- `structlog` or `python-json-logger` — structured JSON logging with correlation IDs

## Orchestration Decision

Use APScheduler with the `ops.job_runs` Postgres table as a job queue for MVP orchestration.

Why this approach fits this app well:

- The `ops.job_runs` table is already designed into the schema with status, attempt count, and error detail fields — no additional infrastructure is needed
- Postgres row locking provides the idempotency and deduplication guarantees required by the acceptance criteria
- APScheduler runs in-process and requires no separate broker service, reducing local development complexity
- The job queue interface can be extracted behind an abstraction layer so the implementation can be upgraded to Celery, Temporal, or another scheduler if throughput requirements grow

Job queue pattern:

1. Orchestrator inserts a job row into `ops.job_runs` for each pipeline stage on its schedule.
2. Each agent polls for jobs of its type, acquires an atomic row lock, and processes.
3. On completion the agent updates the row with final status and any error detail.
4. The Orchestrator checks upstream job status before inserting dependent downstream jobs.

See `docs/agent_skills.md` for the full skill contracts used by each agent, including the orchestrator job queue helpers.

## Additional Best Practices and Industry Standards

### Data and Time Integrity

- Treat release time, publish time, and ingestion time as separate fields.
- Preserve revision history for economic and event data that can be corrected after publication.
- Mark source freshness and stale-data conditions explicitly.
- Record the exact asset universe and tradability assumptions used for every backtest and live prediction.

### ML and Model Risk Management

- Start with simple benchmark models before using LLM or multi-agent strategies.
- Require every new model or prompt change to beat a baseline on frozen validation windows before promotion.
- Track model drift, calibration drift, and feature drift over time.
- Add champion-challenger evaluation for model upgrades.
- Version prompts, model parameters, and feature sets together so runs are reproducible.

### Security and Reliability

- Sign or authenticate internal service-to-service requests where possible.
- Put rate limiting and circuit breakers around third-party APIs.
- Define dead-letter handling for jobs that repeatedly fail.
- Add outbound Telegram destination validation and provider-specific request validation where supported.

### Analytics and Decision Hygiene

- Separate prediction quality metrics from paper-trading return metrics.
- Include transaction costs, slippage, spread, and liquidity filters in any strategy-style reporting.
- Require minimum sample sizes before showing confidence in a pattern.
- Prefer event studies, ablation tests, and out-of-sample validation before elevating a narrative to a "driver."

### Governance

- Add a manual review mode for newly introduced sources or newly promoted models.
- Maintain a data catalog for each source with owner, freshness expectation, known caveats, and legal usage notes.
- Document what the system is allowed to say versus what requires human review, especially around causation and financial recommendations.
- Keep a clear paper-trading-only boundary until validation thresholds are met for a sustained period.

## Initial Domain Model

- `SourceRecord`
- `NormalizedEvent`
- `FeatureValue`
- `Prediction`
- `PredictionTarget`
- `EvaluationResult`
- `BacktestRun`
- `ModelVersion`
- `FeatureSetVersion`

## Unit Test Plan

### Connector Tests

- maps API payloads into normalized schema correctly
- rejects malformed payloads with explicit errors
- preserves source timestamps and identifiers
- retries transient 429 and 5xx responses with backoff
- deduplicates repeated records from the same source
- versions revised source records instead of overwriting them

### Feature Engineering Tests

- computes rolling features without using future records
- aligns macroeconomic releases to public release timestamps
- handles missing values and sparse event streams
- extracts entities, geography, and topics from text consistently
- produces deterministic output from the same frozen input set
- records lineage from feature to source records

### Prediction Logic Tests

- rejects predictions with missing required fields
- rejects probabilities outside 0 to 1
- creates immutable prediction records
- uses only features available at issuance time
- attaches the correct target definition and forecast horizon
- stores model version and prompt version metadata

### Evaluation Tests

- evaluates predictions only after horizon expiration
- settles equity targets using trading-calendar-aware timestamps
- settles crypto targets using 24/7 timestamps
- computes directional accuracy correctly
- computes Brier score correctly
- bins predictions into calibration buckets correctly
- marks predictions as void when price data is unavailable

### Alerting Tests

- sends a Telegram alert when probability is `>= 0.85` and horizon is 3 days or less
- does not send an alert when probability is below `0.85`
- does not send an alert when horizon is greater than 3 days
- does not send alerts for backtest predictions
- formats the alert payload with the required prediction fields
- retries transient alert delivery failures safely without duplicate alert messages
- records alert success and failure states in the delivery log

### Backtesting Tests

- prevents training/test leakage across windows
- reproduces identical metrics from identical frozen datasets
- labels outputs as backtest rather than live
- stores full experiment metadata
- applies transaction cost assumptions consistently

### Orchestration Tests

- runs pipeline stages in the expected order
- retries idempotent failed stages safely
- does not republish predictions after a worker retry
- propagates structured errors and preserves audit logs
- resumes from checkpoints after a crash

### Causality Guardrail Tests

- labels correlations as correlations
- blocks unsupported causal language in generated summaries
- allows causal language only when required evidence metadata is present
- renders uncertainty warnings when evidence quality is low

## Integration Tests To Plan Early

- end-to-end flow from source ingestion to prediction settlement
- delayed data release scenario
- revised macroeconomic release scenario
- source outage during ingestion window
- market holiday or weekend settlement scenario
- duplicate event storm from a noisy API

## Recommended First Milestone

1. Ingest a small but high-signal set of sources.
2. Normalize events and prices into an append-only store.
3. Define 3 to 5 concrete forecast targets.
4. Generate heuristic predictions before using complex models.
5. Auto-evaluate predictions and expose calibration metrics.
6. Add causal-claim guardrails before any strategy narrative features.

## Public APIs To Consider First

### High Priority

- GDELT DOC 2.0 and related GDELT feeds for global event and media coverage.
- FRED for economic releases and time series.
- EIA API for energy prices, production, inventories, and power data.
- SEC EDGAR APIs for company filings and real-time disclosures.
- CoinGecko for crypto market and market structure data.
- Alpha Vantage or another market data provider for equity, ETF, forex, commodity, and economic series if you want a simpler all-in-one prototype.

### Useful Additions

- World Bank Indicators API for long-run cross-country structural indicators.
- IMF Data APIs for international macro and balance-of-payments style data.
- NOAA Climate Data Online for weather and climate shocks.
- USGS APIs for earthquakes and other geophysical event data.
- NewsAPI for simpler headline ingestion when you want easier article search than raw event coding.

## Hallucination Prevention and Model Quality

The system must guard against LLM-generated predictions that contain fabricated data. Full strategy in `docs/context_and_cost_management.md`.

- Model temperature is fixed at `0.1` (configurable) for all prediction generation calls to minimize stochastic hallucination.
- The system prompt explicitly forbids the model from inventing or estimating any numeric value not present in the feature snapshot.
- All model outputs are parsed against a strict Pydantic schema before acting on them. Malformed outputs trigger a corrective retry (max `MAX_OUTPUT_VALIDATION_RETRIES=2`); after that, the prediction is abandoned.
- An evidence grounding check runs after each prediction is generated, flagging any numeric values in the evidence summary that cannot be traced back to the feature snapshot. Flagged predictions are excluded from alerts and surfaced for operator review.
- Predictions with probability below `0.05` or above `0.95` are flagged as potentially overconfident and excluded from alerts pending review.
- All LLM call outcomes, token counts, utilization percentages, and cost in USD are logged to `ops.model_usage_log` and the Admin UI Dashboard.

## Context Management

The system must detect when an LLM context is approaching its model limit and compress proactively. Full strategy in `docs/context_and_cost_management.md`.

- Before every LLM call, token utilization is estimated against the active model's context window.
- At 75% utilization, soft compression runs: features are ranked by relevance and lowest-scoring ones are dropped with a logged summary.
- At 90% utilization, hard compression runs: a cheap model summarizes the evidence block to 3 sentences; only the top `MAX_FEATURES_CRITICAL=10` features are kept. The resulting prediction is tagged `context_compressed = True`.
- In multi-turn agent loops, a sliding window keeps only the last `CONTEXT_KEEP_TOOL_RESULTS=5` tool results in full; older results are replaced with one-line summaries.

## Cost Control

The system must track and limit LLM inference spend. Full strategy in `docs/context_and_cost_management.md`.

- Every LLM call logs input tokens, output tokens, context utilization, and cost in USD to `ops.model_usage_log`.
- Configurable per-run and daily spend caps (`MAX_SPEND_PER_RUN_USD`, `MAX_SPEND_DAILY_USD`) halt prediction generation if exceeded.
- Tiered model routing uses cheap models (Haiku, `llama3.2`) for high-volume normalization and extraction; higher-quality models only for prediction generation.
- Normalization calls are batched (`NORMALIZATION_BATCH_SIZE=10`) to amortize system prompt token cost.
- Anthropic prompt caching is enabled for static system prompt content, targeting 80%+ cache hit rate in steady state.
- Pre-LLM fuzzy deduplication removes semantically duplicate news articles before they incur normalization cost.

## Admin UI and Operator Controls

The system must provide a browser-based Admin UI at `/admin` for operators to adjust configurable levers without code changes or direct database access. Full specification in `docs/admin_ui.md`.

Required configurable levers accessible through the UI:

- Pipeline safety limits: max agent tool calls, max agent iterations (self-analysis passes), max input/output tokens, max features per prediction.
- Per-source settings: enable/disable, trust level, rate limit, retry attempts, ingest interval.
- Add new data sources: operators can register a new API source through the UI; new sources default to `unverified` and do not influence predictions until explicitly promoted to `verified`.
- Model settings: provider, model name, token limits, API keys (write-only).
- Alert rules: probability threshold, horizon limit, Telegram destination, global enable/disable.
- Prediction targets: add and deactivate forecast targets.
- Dead-letter queue: inspect, requeue, or dismiss stuck jobs.

All admin changes are written to `ops.audit_logs` with before/after values and the acting user's identity. Secrets (API keys, bot tokens) are never displayed after initial save.

A persistent `ops.config_overrides` table stores UI-applied settings so they survive restarts. Resolution order: DB overrides > `.env` > coded defaults.

## Input Validation and Security

All incoming data — from external APIs and from operator input in the Admin UI — is untrusted until it passes validation. Full strategy in `docs/input_validation_and_security.md`.

- Every raw API payload passes through a sequential `ValidatorPipeline` (size, encoding, structural, range, temporal, anomaly, duplicate) before any database write.
- Records that fail validation are written with `validation_status = quarantined` or `rejected` and a `validation_errors` JSON field. Nothing is silently discarded.
- Only records from `verified` sources with `validation_status = valid` proceed to normalization and feature generation.
- Prompt injection defense is mandatory: external text is never concatenated directly into LLM prompts. It is passed as a clearly labeled, sanitized data block. The system prompt instructs the model to treat it as data, not instructions.
- All LLM outputs are validated against a Pydantic schema before acting on them. Malformed outputs are rejected and logged.
- SSRF prevention: all operator-supplied URLs (API base URLs) are validated against a private IP blocklist before the system makes any outbound request.
- SQL injection: asyncpg parameterized queries are required throughout. Raw SQL string interpolation is blocked by a CI lint rule.
- XSS prevention: Jinja2 `autoescape=True` is set globally; all user-supplied text is HTML-escaped before rendering.
- API keys, bot tokens, chat IDs, and credentials are never written to logs at any level.

## AI Processing Safety

The system must prevent infinite loops and runaway processing at both the agent level and the job level. Full strategy is in `docs/ai_safety_and_loops.md`. Key requirements:

- Every LLM agent invocation is bounded by a hard tool-call budget (`MAX_AGENT_TOOL_CALLS`) and a cycle-detection check.
- All retryable failures follow a three-tier escalation: auto-retry (1–3 attempts), dead-letter plus operator alert (4–5), pipeline pause requiring human review (6+).
- Human-in-the-loop escalation is triggered automatically for: connector down > 24 hours, prediction calibration collapse, evaluation void rate > 50% in 24 hours, loop detected in any agent.
- Feature snapshots are trimmed to `MAX_FEATURES_PER_PREDICTION` before being passed to any LLM to prevent context overflow.
- A migration is required to add `attempt_count` and `max_attempts` columns to `ops.job_runs` before the backoff strategy can be fully enforced.

## Model Configuration

The system uses a provider-agnostic `ModelClient` interface so the AI model can be swapped without code changes. Full strategy is in `docs/model_configuration.md`.

- Default for development: Ollama (local, free). Recommended starting model: `llama3.2:8b`.
- Recommended for staging: Groq free tier (`llama-3.3-70b-versatile`).
- Recommended for production: Anthropic Claude (`claude-haiku-4-5-20251001` for normalization, `claude-sonnet-4-6` for prediction engine).
- The baseline heuristic prediction engine must be implemented before any LLM-based engine. Validate that the LLM beats the baseline before promoting it.

## Cron and Discovery Schedule

The system runs on a scheduled cron cadence for each pipeline stage:

| Stage | Interval | Purpose |
|---|---|---|
| Discovery | Every 12 hours | API health check, schema change detection, new asset discovery |
| Price ingestion | Every 15 minutes | Ingest latest price bars from market data sources |
| News/event ingestion | Every 1 hour | Ingest latest news and event records |
| Macro data check | Every 6 hours | Check for new economic releases |
| Prediction run | Triggered after feature generation | Generate predictions for active targets |
| Evaluation run | Every 24 hours | Settle predictions past their horizon end time |
| Alert check | Every 1 hour | Send Telegram alerts for new high-confidence live predictions |

## Logging Requirements

All services must emit structured JSON logs with a shared correlation ID. Full specification in `docs/logging_strategy.md`.

- Log format: single-line JSON per event (no multi-line stack traces).
- Log location: `./logs/app.log` (local dev), stdout (Docker/production), configurable via `LOG_FILE_PATH`.
- Every log line includes: `timestamp`, `level`, `correlation_id`, `agent`, `source`, `message`.
- Correlation ID is generated by the Orchestrator and propagated to every stage of a single pipeline run.
- Retention: DEBUG 7 days, INFO 30 days, ERROR/CRITICAL 90 days, audit log indefinite.

## Input and Output Specification

Full I/O shapes are defined in `docs/io_specification.md`.

- Input: raw API payloads from external sources (events, news, macro, price bars), mapped into canonical `RawSourceRecord` format on ingestion.
- Intermediate: normalized events with sentiment, entities, topics, and geography; feature snapshots with full lineage and point-in-time cutoff.
- Output: immutable prediction records with `probability` in range `[0.00, 1.00]`, stored as `NUMERIC(6,5)` and displayed to 2 decimal places (e.g. `0.87`).
- Alert output: Telegram alert message including prediction ID, asset, direction, threshold, horizon, and probability, labeled with causation warning if claim type is `correlation`.

## Agent Skills Reference

Each agent's required tool functions are documented in `docs/agent_skills.md`. That document covers:

- Tool signatures and purpose for every agent (Source Connector, Normalization, Feature Engineering, Prediction Engine, Evaluation Engine, Alerting, Orchestrator)
- Shared cross-agent utilities
- The `BaseConnector` interface all source connectors must implement
- The recommended Postgres job queue communication pattern

Tool contracts in `docs/agent_skills.md` map directly to the unit tests in `docs/unit_test_plan.md` — each skill function corresponds to one or more test cases.

## Notes On Data Strategy

- For causal research, prefer event-coded and release-timestamped data over raw headline counts alone.
- For economic prediction, release calendars and revision histories matter almost as much as the values themselves.
- For short-term trading predictions, market microstructure and liquidity features often dominate broad macro variables except around major scheduled releases or shocks.
- Start with interpretable baseline models and event studies before training complex agents.
