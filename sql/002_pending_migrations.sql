-- Migration 002: schema additions required before implementation begins.
-- Apply AFTER 001_init_experimental_prediction_app.sql
-- All ALTER TABLE statements use IF NOT EXISTS / IF EXISTS where supported
-- to make migrations re-runnable during development.

-- ============================================================
-- 1. ops.job_runs — attempt tracking for three-tier backoff
-- ============================================================
ALTER TABLE ops.job_runs
    ADD COLUMN attempt_count integer NOT NULL DEFAULT 0,
    ADD COLUMN max_attempts  integer NOT NULL DEFAULT 3;

ALTER TABLE ops.job_runs
    ADD CONSTRAINT job_runs_nonnegative_attempts CHECK (attempt_count >= 0),
    ADD CONSTRAINT job_runs_time_order CHECK (finished_at IS NULL OR finished_at >= started_at);

CREATE INDEX job_runs_status_idx
    ON ops.job_runs (status, started_at DESC);

-- ============================================================
-- 2. ingestion.raw_source_records — validation pipeline columns
--    and version chaining (prior_record_id for revised records)
-- ============================================================
ALTER TABLE ingestion.raw_source_records
    ADD COLUMN prior_record_id  uuid REFERENCES ingestion.raw_source_records(id),
    ADD COLUMN validation_status text NOT NULL DEFAULT 'pending',
    ADD COLUMN validation_errors   jsonb,
    ADD COLUMN validation_warnings jsonb;

ALTER TABLE ingestion.raw_source_records
    ADD CONSTRAINT raw_source_records_validation_status_check
        CHECK (validation_status IN ('pending', 'valid', 'quarantined', 'rejected'));

CREATE INDEX raw_source_records_validation_status_idx
    ON ingestion.raw_source_records (validation_status, ingested_at DESC);

-- prior_record_id index for version chain traversal
CREATE INDEX raw_source_records_prior_record_idx
    ON ingestion.raw_source_records (prior_record_id)
    WHERE prior_record_id IS NOT NULL;

-- ============================================================
-- 3. features.feature_values — available_at for anti-lookahead
--    This is the critical column that validate_no_future_leak
--    enforces: every value must have available_at < issuance_time.
-- ============================================================
ALTER TABLE features.feature_values
    ADD COLUMN available_at timestamptz;

-- Partial index: fast lookup of features available before a cutoff time
CREATE INDEX feature_values_available_at_idx
    ON features.feature_values (available_at DESC)
    WHERE available_at IS NOT NULL;

-- ============================================================
-- 4. ingestion.normalized_events — missing indexes and constraints
-- ============================================================
CREATE INDEX normalized_events_source_record_idx
    ON ingestion.normalized_events (source_record_id);

ALTER TABLE ingestion.normalized_events
    ADD CONSTRAINT normalized_events_event_type_check
        CHECK (event_type IN ('news', 'economic_release', 'geopolitical_event', 'corporate_filing')),
    ADD CONSTRAINT normalized_events_sentiment_range
        CHECK (sentiment_score IS NULL OR sentiment_score BETWEEN -1 AND 1),
    ADD CONSTRAINT normalized_events_severity_range
        CHECK (severity_score IS NULL OR severity_score BETWEEN 0 AND 1);

-- ============================================================
-- 5. features.feature_lineage — missing indexes
--    Table exists from migration 001 but has no indexes.
-- ============================================================
CREATE INDEX feature_lineage_snapshot_idx
    ON features.feature_lineage (snapshot_id);

CREATE INDEX feature_lineage_source_record_idx
    ON features.feature_lineage (source_record_id)
    WHERE source_record_id IS NOT NULL;

CREATE INDEX feature_lineage_normalized_event_idx
    ON features.feature_lineage (normalized_event_id)
    WHERE normalized_event_id IS NOT NULL;

-- ============================================================
-- 6. features.feature_snapshots — asset_id index
-- ============================================================
CREATE INDEX feature_snapshots_asset_id_idx
    ON features.feature_snapshots (asset_id);

-- ============================================================
-- 7. market_data.assets — asset_type check constraint and index
-- ============================================================
ALTER TABLE market_data.assets
    ADD CONSTRAINT assets_asset_type_check
        CHECK (asset_type IN ('crypto', 'equity', 'commodity', 'forex', 'index', 'etf'));

CREATE INDEX assets_symbol_asset_type_idx
    ON market_data.assets (symbol, asset_type);

-- ============================================================
-- 8. ops.api_sources — source trust model and per-source overrides
-- ============================================================
ALTER TABLE ops.api_sources
    ADD COLUMN trust_level               text    NOT NULL DEFAULT 'unverified',
    ADD COLUMN rate_limit_per_minute     integer,
    ADD COLUMN max_record_age_days       integer DEFAULT 90,
    -- NULL on max_record_age_days means "use global default (90 days)".
    -- Set to 0 for historical/backfill sources with no age limit.
    ADD COLUMN max_attempts              integer,
    -- NULL means "use global DEFAULT_JOB_MAX_ATTEMPTS from config_overrides / .env".
    ADD COLUMN ingest_cron_interval_seconds integer,
    -- NULL means "use the global cron interval for this source category".
    ADD COLUMN notes                     text,
    ADD COLUMN updated_at                timestamptz;

ALTER TABLE ops.api_sources
    ADD CONSTRAINT api_sources_trust_level_check
        CHECK (trust_level IN ('verified', 'unverified', 'quarantine')),
    ADD CONSTRAINT api_sources_auth_type_check
        CHECK (auth_type IN ('none', 'api_key', 'bearer', 'basic')),
    ADD CONSTRAINT api_sources_category_check
        CHECK (category IN ('events', 'news', 'macro', 'market_data'));

-- ============================================================
-- 9. predictions.prediction_targets — constraints and updated_at
-- ============================================================
ALTER TABLE predictions.prediction_targets
    ADD COLUMN updated_at timestamptz;

ALTER TABLE predictions.prediction_targets
    ADD CONSTRAINT prediction_targets_asset_type_check
        CHECK (asset_type IN ('crypto', 'equity', 'commodity', 'forex'));

CREATE INDEX prediction_targets_is_active_idx
    ON predictions.prediction_targets (is_active)
    WHERE is_active = true;

-- Convert direction_rule and settlement_rule from text to jsonb.
-- Grammar spec is in docs/io_specification.md (direction_rule and settlement_rule schemas).
-- Existing rows (if any) must be valid JSON or this will error — re-seed targets after migration.
ALTER TABLE predictions.prediction_targets
    ALTER COLUMN direction_rule  TYPE jsonb USING direction_rule::jsonb,
    ALTER COLUMN settlement_rule TYPE jsonb USING settlement_rule::jsonb;

-- Add optional asset_id FK for asset-specific targets.
-- NULL = target applies to all assets of the matching asset_type (generic target).
-- Non-null = target is pinned to one specific asset (e.g. BTC/USD only).
ALTER TABLE predictions.prediction_targets
    ADD COLUMN asset_id uuid REFERENCES market_data.assets(id);

CREATE INDEX prediction_targets_asset_id_idx
    ON predictions.prediction_targets (asset_id)
    WHERE asset_id IS NOT NULL;

-- ============================================================
-- 10. predictions.predictions — hallucination flags, correlation_id fix,
--     backtest linkage
-- ============================================================
ALTER TABLE predictions.predictions
    ADD COLUMN hallucination_risk       boolean NOT NULL DEFAULT false,
    ADD COLUMN probability_extreme_flag boolean NOT NULL DEFAULT false,
    ADD COLUMN context_compressed       boolean NOT NULL DEFAULT false,
    ADD COLUMN backtest_run_id          uuid REFERENCES evaluation.backtest_runs(id),
    ADD COLUMN llm_probability         numeric(7, 5),
    ADD COLUMN pre_cal_probability     numeric(7, 5);

-- Fix: correlation_id on predictions should be SET to the orchestrator's
-- pipeline-level correlation_id (same UUID as ops.job_runs.correlation_id),
-- NOT independently generated. Remove the auto-gen default.
ALTER TABLE predictions.predictions
    ALTER COLUMN correlation_id DROP DEFAULT;

CREATE INDEX predictions_hallucination_risk_idx
    ON predictions.predictions (hallucination_risk)
    WHERE hallucination_risk = true;

CREATE INDEX predictions_backtest_run_idx
    ON predictions.predictions (backtest_run_id)
    WHERE backtest_run_id IS NOT NULL;

-- ============================================================
-- 10b. ml.training_examples - clean model-training surface
-- ============================================================
CREATE SCHEMA IF NOT EXISTS ml;

CREATE OR REPLACE VIEW ml.training_examples AS
SELECT
    p.id                        AS prediction_id,
    p.target_id,
    p.asset_id,
    p.feature_snapshot_id,
    p.created_at,
    p.model_version_id,
    p.prediction_mode,
    p.llm_probability,
    p.pre_cal_probability,
    p.probability               AS final_probability,
    p.hallucination_risk,
    p.probability_extreme_flag,
    er.directional_correct,
    er.brier_score,
    er.return_pct,
    er.cost_adjusted_return_pct,
    er.calibration_bucket,
    er.actual_outcome,
    er.evaluated_at
FROM predictions.predictions p
JOIN evaluation.evaluation_results er
    ON er.prediction_id = p.id
   AND er.evaluation_state = 'evaluated';

-- ============================================================
-- 11. ops.alert_rules — updated_at for change tracking
-- ============================================================
ALTER TABLE ops.alert_rules
    ADD COLUMN updated_at timestamptz;

CREATE INDEX alert_rules_is_active_idx
    ON ops.alert_rules (is_active)
    WHERE is_active = true;

-- ============================================================
-- 12. ops.alert_deliveries — sanity constraint
-- ============================================================
ALTER TABLE ops.alert_deliveries
    ADD CONSTRAINT alert_deliveries_nonneg_attempts CHECK (attempt_count >= 0);

-- ============================================================
-- 13. ops.config_overrides — persistent Admin UI settings
--     Resolution order: this table > .env > coded default
-- ============================================================
CREATE TABLE ops.config_overrides (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key           text NOT NULL UNIQUE,
    value         text NOT NULL,
    default_value text NOT NULL,
    description   text,
    updated_by    text NOT NULL DEFAULT 'system',
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Seed the known configurable keys with documented defaults.
-- Applications read from this table first; missing keys fall back to .env.
INSERT INTO ops.config_overrides (key, value, default_value, description) VALUES
    ('MAX_AGENT_TOOL_CALLS',          '20',     '20',     'Hard limit on tool calls per LLM agent invocation'),
    ('MAX_AGENT_ITERATIONS',          '5',      '5',      'Max reasoning iterations (self-analysis passes) per prediction'),
    ('MAX_AGENT_INPUT_TOKENS',        '6000',   '6000',   'Max tokens fed to the model per call'),
    ('MAX_AGENT_OUTPUT_TOKENS',       '2000',   '2000',   'Max tokens the model may generate per call'),
    ('MAX_FEATURES_PER_PREDICTION',   '25',     '25',     'Feature values passed to the prediction model per snapshot'),
    ('MAX_EVIDENCE_SUMMARY_CHARS',    '1000',   '1000',   'Max length of evidence summary text'),
    ('JOB_MAX_RUNTIME_SECONDS',       '300',    '300',    'Global job timeout in seconds'),
    ('DEFAULT_JOB_MAX_ATTEMPTS',      '3',      '3',      'Default retry budget before dead-letter'),
    ('ALERT_MIN_PROBABILITY',         '0.85',   '0.85',   'Minimum probability to trigger a Telegram alert'),
    ('ALERT_MAX_HORIZON_HOURS',       '72',     '72',     'Maximum horizon (wall-clock hours) for alertable predictions'),
    ('CONTEXT_WARNING_THRESHOLD_PCT', '0.75',   '0.75',   'Begin soft compression at this context utilization fraction'),
    ('CONTEXT_CRITICAL_THRESHOLD_PCT','0.90',   '0.90',   'Hard compress at this context utilization fraction'),
    ('MAX_FEATURES_CRITICAL',         '10',     '10',     'Feature count kept after hard compression'),
    ('NORMALIZATION_BATCH_SIZE',      '10',     '10',     'Records per normalization LLM call'),
    ('MAX_SPEND_PER_RUN_USD',         '0.50',   '0.50',   'Spend cap per prediction batch run (USD)'),
    ('MAX_SPEND_DAILY_USD',           '5.00',   '5.00',   'Daily spend cap across all runs (USD)'),
    ('MODEL_TEMPERATURE',             '0.1',    '0.1',    'Sampling temperature for prediction generation calls'),
    ('HALLUCINATION_PROB_LOW',        '0.05',   '0.05',   'Flag predictions with probability below this value'),
    ('HALLUCINATION_PROB_HIGH',       '0.95',   '0.95',   'Flag predictions with probability above this value'),
    ('MAX_OUTPUT_VALIDATION_RETRIES', '2',      '2',      'Max retries for malformed model output before abandoning'),
    ('SELF_CONSISTENCY_ENABLED',      'false',  'false',  'Run prediction N times and compare for consistency');

-- ============================================================
-- 14. ops.discovery_snapshots — persists API payload shapes for schema-change detection.
--     The Discovery Agent compares the current sample payload against the most recent
--     snapshot for each source to identify added, removed, or changed fields.
-- ============================================================
CREATE TABLE ops.discovery_snapshots (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id        uuid NOT NULL REFERENCES ops.api_sources(id),
    snapshot_at      timestamptz NOT NULL DEFAULT now(),
    payload_shape    jsonb NOT NULL,
    -- payload_shape: {"field_name": "type_string", ...} — top-level key/type map of a sample payload
    schema_version   text,
    -- Optional: connector-provided API version string if the source exposes one
    fields_added     jsonb NOT NULL DEFAULT '[]',
    fields_removed   jsonb NOT NULL DEFAULT '[]',
    fields_changed   jsonb NOT NULL DEFAULT '[]',
    -- Each is a JSON array of field name strings changed since the prior snapshot
    is_breaking      boolean NOT NULL DEFAULT false,
    -- True when fields_removed is non-empty or a required field type changed
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT discovery_snapshots_unique_source_at UNIQUE (source_id, snapshot_at)
);

CREATE INDEX discovery_snapshots_source_idx
    ON ops.discovery_snapshots (source_id, snapshot_at DESC);

-- ============================================================
-- 15. ops.model_usage_log — LLM call cost and context tracking
-- ============================================================
CREATE TABLE ops.model_usage_log (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id          uuid,
    job_run_id              uuid REFERENCES ops.job_runs(id),
    agent                   text NOT NULL,
    model_provider          text NOT NULL,
    model_name              text NOT NULL,
    call_purpose            text NOT NULL,
    -- Valid call_purpose values: normalization | entity_extraction | sentiment |
    -- prediction_generation | evidence_grounding_check |
    -- context_compression_summary | self_consistency_run
    input_tokens            integer NOT NULL,
    output_tokens           integer NOT NULL,
    context_window_tokens   integer NOT NULL,
    context_utilization_pct numeric(5, 2) NOT NULL,
    compression_applied     boolean NOT NULL DEFAULT false,
    compression_type        text,
    -- compression_type: 'soft' | 'hard' | null
    cost_usd                numeric(10, 6) NOT NULL DEFAULT 0,
    duration_ms             integer,
    output_valid            boolean NOT NULL DEFAULT true,
    hallucination_flags     jsonb NOT NULL DEFAULT '{}',
    -- hallucination_flags keys: evidence_grounding_failed (bool),
    -- probability_extreme (bool), output_validation_retries (int)
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX model_usage_log_correlation_idx
    ON ops.model_usage_log (correlation_id, created_at DESC);

CREATE INDEX model_usage_log_created_idx
    ON ops.model_usage_log (created_at DESC);

CREATE INDEX model_usage_log_job_run_idx
    ON ops.model_usage_log (job_run_id)
    WHERE job_run_id IS NOT NULL;
