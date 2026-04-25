# Prediction App Unit Test Plan

## Goals

- Prevent lookahead bias and timestamp mistakes.
- Preserve immutability and auditability of predictions.
- Ensure probabilistic predictions are valid and evaluable.
- Keep alerting, orchestration, and external connector behavior deterministic.

## Test Strategy

- Write fast unit tests around pure normalization, feature, scoring, and rule logic.
- Use fixture-based tests for connector payload mapping.
- Use database tests for constraints, idempotency, and migration-sensitive behavior.
- Keep integration tests focused on a small number of end-to-end critical paths.

## Priority Levels

- `P0`: correctness bugs that can invalidate predictions or alerts
- `P1`: failures that break pipeline reliability or traceability
- `P2`: lower-risk analytics, reporting, or operator workflow issues

## Connector Tests

### P0

- map valid source payloads into the normalized schema
- preserve `source_recorded_at`, `released_at`, `published_at`, and `ingested_at`
- reject malformed or incomplete payloads with explicit errors
- deduplicate records from the same source and external id
- version corrected source records instead of overwriting them

### P1

- retry transient `429` and `5xx` failures with bounded backoff
- stop retrying on permanent `4xx` failures
- record connector failure context in job logs
- handle empty responses without crashing downstream steps

## Feature Engineering Tests

### P0

- compute rolling features using only records available before prediction issuance time
- align macroeconomic indicators to public release timestamps rather than observation periods
- build deterministic feature snapshots from frozen input sets
- record lineage from each feature value back to source records

### P1

- handle sparse event streams and missing values consistently
- normalize entities, geography, and topics with repeatable outputs
- avoid duplicate feature rows for the same snapshot and key

## Prediction Logic Tests

### P0

- reject predictions with missing required fields
- reject probabilities below `0` or above `1`
- attach the correct prediction target and forecast horizon
- create immutable prediction rows once published
- block predictions that reference feature data newer than the prediction timestamp

### P1

- persist model version, prompt version, and feature set version metadata
- classify live versus backtest predictions correctly
- preserve evidence summaries and structured rationale fields

## Evaluation Tests

### P0

- evaluate predictions only after the horizon end timestamp
- settle equity targets with trading-calendar-aware timestamps
- settle crypto targets with continuous 24/7 timestamps
- compute directional accuracy correctly
- compute Brier score correctly
- compute calibration bucket assignment correctly
- record `created_at` and `horizon_end_at` timestamps on every prediction at write time

### P1

- mark predictions as `void` when required price data is missing
- keep settlement logic stable across weekends and market holidays
- apply transaction cost and slippage assumptions consistently in paper metrics

## Accuracy Reporting Tests

### P0

- exclude predictions newer than 3 days from accuracy summary (not yet resolved)
- exclude backtest predictions from all accuracy metrics
- compute directional accuracy percentage correctly from evaluated results
- compute mean Brier score correctly from evaluated results
- return `None` from `compute_accuracy_summary` when no qualifying evaluated predictions exist
- do not send a Telegram message when there are no qualifying results
- format accuracy report message with total count, directional accuracy %, mean Brier score, and per-target rows

### P1

- send accuracy report only when new evaluations were settled in the current run, not on every tick
- include per-target breakdown sorted by directional accuracy descending
- handle single-prediction window without crashing (n=1 edge case)
- log success and failure of Telegram delivery for the accuracy report

## Alerting Tests

### P0

- send a Telegram alert when probability is `>= 0.85` and horizon is `<= 3 days`
- do not send alerts when probability is below `0.85`
- do not send alerts when horizon is greater than `3 days`
- do not send alerts for backtest predictions
- include required fields in the alert payload

### P1

- retry transient Telegram delivery failures without duplicating successful alert messages
- log success and failure attempts for each alert delivery
- allow threshold changes from configuration without code changes

## Database Constraint Tests

### P0

- enforce uniqueness for source plus external id plus version
- enforce prediction probability bounds
- enforce foreign keys between predictions, targets, models, snapshots, and evaluations
- prevent duplicate alert deliveries for the same prediction and rule

### P1

- verify audit log inserts for prediction issuance and evaluation actions
- verify default timestamps and generated UUID behavior

## Orchestration Tests

### P0

- execute jobs in the correct dependency order
- retry idempotent jobs without duplicate predictions or alerts
- resume safely after worker interruption

### P1

- emit correlation ids through all stages of a single prediction flow
- move repeatedly failing jobs into dead-letter handling
- preserve structured error details for operator review

## Integration Tests To Add Early

- ingest one source record through normalization, feature generation, prediction, evaluation, and alert gating
- handle delayed macro data releases without leaking future information
- handle a noisy duplicate event feed without duplicate predictions
- settle a prediction across a weekend boundary
- replay a historical backtest batch separately from live predictions

## Suggested Test Data Fixtures

- one revised macroeconomic release
- one late-arriving breaking-news event
- one weekend crypto prediction
- one Friday equity prediction with Monday settlement
- one high-confidence live prediction that should alert
- one similar backtest prediction that should not alert

## Exit Criteria For MVP

- all `P0` unit tests pass in CI
- critical integration smoke tests pass in CI
- database migration can be applied to a clean Postgres instance
- alerting behavior is verified against a test Telegram destination
