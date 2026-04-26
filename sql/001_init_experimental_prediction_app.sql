-- Bootstrap script for the experimental_prediction_app PostgreSQL database.
-- Usage (standalone):
--   psql -U <user> -f sql/001_init_experimental_prediction_app.sql
-- In Docker: run against the already-created POSTGRES_DB database; the CREATE DATABASE
-- and \connect lines are skipped automatically because Docker's entrypoint executes this
-- script with -d experimental_prediction_app already set.

SELECT 'CREATE DATABASE experimental_prediction_app'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'experimental_prediction_app'
)\gexec

\connect experimental_prediction_app;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS ingestion;
CREATE SCHEMA IF NOT EXISTS market_data;
CREATE SCHEMA IF NOT EXISTS features;
CREATE SCHEMA IF NOT EXISTS predictions;
CREATE SCHEMA IF NOT EXISTS evaluation;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE ops.api_sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    category text NOT NULL,
    base_url text,
    auth_type text NOT NULL DEFAULT 'none',
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE market_data.assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol text NOT NULL,
    asset_type text NOT NULL,
    name text,
    exchange text,
    base_currency text,
    quote_currency text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assets_symbol_asset_type_exchange_key UNIQUE (symbol, asset_type, exchange)
);

CREATE TABLE ingestion.raw_source_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL REFERENCES ops.api_sources(id),
    external_id text NOT NULL,
    record_version integer NOT NULL DEFAULT 1,
    source_recorded_at timestamptz,
    released_at timestamptz,
    published_at timestamptz,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    raw_payload jsonb NOT NULL,
    normalized_payload jsonb,
    checksum text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT raw_source_records_unique_source_external_version UNIQUE (source_id, external_id, record_version)
);

CREATE INDEX raw_source_records_source_recorded_at_idx
    ON ingestion.raw_source_records (source_recorded_at DESC);

CREATE INDEX raw_source_records_ingested_at_idx
    ON ingestion.raw_source_records (ingested_at DESC);

CREATE TABLE ingestion.normalized_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_record_id uuid NOT NULL REFERENCES ingestion.raw_source_records(id) ON DELETE CASCADE,
    event_type text NOT NULL,
    event_subtype text,
    title text,
    summary text,
    sentiment_score numeric(8, 4),
    severity_score numeric(8, 4),
    country_code text,
    region text,
    entity_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    event_occurred_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX normalized_events_event_type_idx
    ON ingestion.normalized_events (event_type, event_occurred_at DESC);

CREATE TABLE market_data.price_bars (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id uuid NOT NULL REFERENCES market_data.assets(id),
    source_id uuid REFERENCES ops.api_sources(id),
    bar_interval text NOT NULL,
    bar_start_at timestamptz NOT NULL,
    bar_end_at timestamptz NOT NULL,
    open numeric(20, 8) NOT NULL,
    high numeric(20, 8) NOT NULL,
    low numeric(20, 8) NOT NULL,
    close numeric(20, 8) NOT NULL,
    volume numeric(28, 8),
    vwap numeric(20, 8),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT price_bars_unique_asset_interval_start UNIQUE (asset_id, bar_interval, bar_start_at),
    CONSTRAINT price_bars_valid_range CHECK (bar_end_at > bar_start_at),
    CONSTRAINT price_bars_nonnegative_volume CHECK (volume IS NULL OR volume >= 0)
);

CREATE INDEX price_bars_asset_end_idx
    ON market_data.price_bars (asset_id, bar_end_at DESC);

CREATE TABLE features.feature_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    version text NOT NULL,
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT feature_sets_name_version_key UNIQUE (name, version)
);

CREATE TABLE features.feature_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_set_id uuid NOT NULL REFERENCES features.feature_sets(id),
    asset_id uuid REFERENCES market_data.assets(id),
    as_of_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    lineage_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT feature_snapshots_unique_set_asset_as_of UNIQUE (feature_set_id, asset_id, as_of_at)
);

CREATE INDEX feature_snapshots_as_of_idx
    ON features.feature_snapshots (as_of_at DESC);

CREATE TABLE features.feature_values (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id uuid NOT NULL REFERENCES features.feature_snapshots(id) ON DELETE CASCADE,
    feature_key text NOT NULL,
    feature_type text NOT NULL,
    numeric_value numeric(24, 8),
    text_value text,
    boolean_value boolean,
    json_value jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT feature_values_unique_snapshot_key UNIQUE (snapshot_id, feature_key)
);

CREATE INDEX feature_values_feature_key_idx
    ON features.feature_values (feature_key);

CREATE TABLE features.feature_lineage (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id uuid NOT NULL REFERENCES features.feature_snapshots(id) ON DELETE CASCADE,
    source_record_id uuid REFERENCES ingestion.raw_source_records(id),
    normalized_event_id uuid REFERENCES ingestion.normalized_events(id),
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE predictions.model_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    version text NOT NULL,
    model_type text NOT NULL,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT model_versions_name_version_key UNIQUE (name, version)
);

CREATE TABLE predictions.prompt_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    version text NOT NULL,
    prompt_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT prompt_versions_name_version_key UNIQUE (name, version)
);

CREATE TABLE predictions.prediction_targets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    asset_type text NOT NULL,
    target_metric text NOT NULL,
    direction_rule text NOT NULL,
    horizon_hours integer NOT NULL,
    settlement_rule text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT prediction_targets_positive_horizon CHECK (horizon_hours > 0)
);

CREATE TABLE predictions.predictions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    target_id uuid NOT NULL REFERENCES predictions.prediction_targets(id),
    asset_id uuid NOT NULL REFERENCES market_data.assets(id),
    feature_snapshot_id uuid NOT NULL REFERENCES features.feature_snapshots(id),
    model_version_id uuid NOT NULL REFERENCES predictions.model_versions(id),
    prompt_version_id uuid REFERENCES predictions.prompt_versions(id),
    prediction_mode text NOT NULL,
    predicted_outcome text NOT NULL,
    probability numeric(6, 5) NOT NULL,
    evidence_summary text NOT NULL,
    rationale jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    horizon_end_at timestamptz NOT NULL,
    correlation_id uuid DEFAULT gen_random_uuid(),
    CONSTRAINT predictions_probability_range CHECK (probability >= 0 AND probability <= 1),
    CONSTRAINT predictions_horizon_after_created CHECK (horizon_end_at > created_at),
    CONSTRAINT predictions_mode_check CHECK (prediction_mode IN ('live', 'backtest'))
);

CREATE INDEX predictions_asset_created_idx
    ON predictions.predictions (asset_id, created_at DESC);

CREATE INDEX predictions_horizon_end_idx
    ON predictions.predictions (horizon_end_at);

CREATE INDEX predictions_probability_idx
    ON predictions.predictions (probability DESC);

CREATE TABLE predictions.prediction_status_history (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id uuid NOT NULL REFERENCES predictions.predictions(id) ON DELETE CASCADE,
    status text NOT NULL,
    reason text,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prediction_status_history_prediction_idx
    ON predictions.prediction_status_history (prediction_id, recorded_at DESC);

CREATE TABLE evaluation.evaluation_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id uuid NOT NULL UNIQUE REFERENCES predictions.predictions(id) ON DELETE CASCADE,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    evaluation_state text NOT NULL,
    actual_outcome text,
    directional_correct boolean,
    brier_score numeric(10, 6),
    return_pct numeric(10, 6),
    cost_adjusted_return_pct numeric(10, 6),
    calibration_bucket text,
    notes text,
    CONSTRAINT evaluation_state_check CHECK (
        evaluation_state IN ('not_evaluable', 'evaluated', 'void', 'superseded_target_definition')
    )
);

CREATE INDEX evaluation_results_state_idx
    ON evaluation.evaluation_results (evaluation_state, evaluated_at DESC);

CREATE TABLE evaluation.backtest_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    code_version text NOT NULL,
    model_version_id uuid NOT NULL REFERENCES predictions.model_versions(id),
    feature_set_id uuid NOT NULL REFERENCES features.feature_sets(id),
    train_start_at timestamptz NOT NULL,
    train_end_at timestamptz NOT NULL,
    test_start_at timestamptz NOT NULL,
    test_end_at timestamptz NOT NULL,
    target_id uuid REFERENCES predictions.prediction_targets(id),
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backtest_train_window_check CHECK (train_end_at > train_start_at),
    CONSTRAINT backtest_test_window_check CHECK (test_end_at > test_start_at),
    CONSTRAINT backtest_window_order_check CHECK (test_start_at >= train_end_at)
);

CREATE TABLE ops.alert_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    min_probability numeric(6, 5) NOT NULL DEFAULT 0.85,
    max_horizon_hours integer NOT NULL DEFAULT 72,
    channel_type text NOT NULL DEFAULT 'telegram',
    destination text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT alert_rules_probability_range CHECK (min_probability >= 0 AND min_probability <= 1),
    CONSTRAINT alert_rules_positive_horizon CHECK (max_horizon_hours > 0)
);

CREATE TABLE ops.alert_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id uuid NOT NULL REFERENCES predictions.predictions(id) ON DELETE CASCADE,
    alert_rule_id uuid NOT NULL REFERENCES ops.alert_rules(id) ON DELETE CASCADE,
    delivery_status text NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    last_attempt_at timestamptz,
    last_error text,
    provider_message_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT alert_deliveries_status_check CHECK (delivery_status IN ('pending', 'sent', 'failed')),
    CONSTRAINT alert_deliveries_unique_prediction_rule UNIQUE (prediction_id, alert_rule_id)
);

CREATE INDEX alert_deliveries_status_idx
    ON ops.alert_deliveries (delivery_status, last_attempt_at DESC);

CREATE TABLE ops.job_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name text NOT NULL,
    correlation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    status text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error_summary text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT job_runs_status_check CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead_letter'))
);

CREATE INDEX job_runs_job_name_started_idx
    ON ops.job_runs (job_name, started_at DESC);

CREATE TABLE ops.audit_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    action text NOT NULL,
    correlation_id uuid,
    actor_type text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_logs_entity_idx
    ON ops.audit_logs (entity_type, entity_id, created_at DESC);

INSERT INTO ops.alert_rules (name, min_probability, max_horizon_hours, channel_type, destination)
VALUES ('default_telegram_high_confidence', 0.85, 72, 'telegram', 'REPLACE_WITH_TELEGRAM_CHAT_ID');
