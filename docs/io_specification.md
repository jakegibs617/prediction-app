# Input/Output Specification

This document defines the canonical shape of data at each stage of the pipeline — from raw API input through normalized intermediates to the final prediction output.

---

## Overview

```
[External APIs]
      │
      ▼
[Raw API Input] ──► ingestion.raw_source_records
      │
      ▼
[Normalized Events / Price Bars]
      │
      ▼
[Feature Snapshot] ──► features.feature_snapshots + feature_values
      │
      ▼
[Prediction Engine Input]
      │
      ▼
[Prediction Output] ──► predictions.predictions
      │
      ▼
[Alert Output] ──► ops.alert_deliveries → Telegram
```

---

## Stage 1: Raw API Input

Each source connector fetches raw records from an external API and maps them into a canonical `RawSourceRecord` before writing to the database.

### RawSourceRecord

| Field | Type | Description |
|---|---|---|
| `source_name` | string | Registered source name (e.g. `alpha_vantage`, `fred`, `newsapi`) |
| `external_id` | string | Stable identifier from the source (e.g. article ID, series+date) |
| `record_version` | integer | 1 for initial; incremented when source revises the record |
| `source_recorded_at` | datetime (UTC) | When the event occurred or observation period ended |
| `released_at` | datetime (UTC) | When the source first published or released the data |
| `published_at` | datetime (UTC) | When the record was available to the public (may differ from released_at) |
| `ingested_at` | datetime (UTC) | When this system fetched the record |
| `raw_payload` | JSON | Verbatim API response, unmodified |
| `checksum` | string | SHA-256 of `raw_payload` for change detection on revised records |

### Example: Alpha Vantage price bar

```json
{
  "source_name": "alpha_vantage",
  "external_id": "BTC/USD::2026-04-18T14:00:00Z::1h",
  "record_version": 1,
  "source_recorded_at": "2026-04-18T14:59:59Z",
  "released_at": "2026-04-18T15:00:01Z",
  "published_at": "2026-04-18T15:00:01Z",
  "ingested_at": "2026-04-18T15:01:03Z",
  "raw_payload": {
    "open": "83210.50",
    "high": "83550.00",
    "low": "83100.00",
    "close": "83420.75",
    "volume": "142.88"
  }
}
```

### Example: FRED macro indicator

```json
{
  "source_name": "fred",
  "external_id": "CPIAUCSL::2026-03-01",
  "record_version": 1,
  "source_recorded_at": "2026-03-01T00:00:00Z",
  "released_at": "2026-04-10T12:30:00Z",
  "published_at": "2026-04-10T12:30:00Z",
  "ingested_at": "2026-04-10T12:45:00Z",
  "raw_payload": {
    "series_id": "CPIAUCSL",
    "observation_date": "2026-03-01",
    "value": "312.840",
    "vintage_date": "2026-04-10"
  }
}
```

Note: `source_recorded_at` is the observation period; `released_at` is when FRED published the figure. These are intentionally different — using `released_at` as the feature availability time prevents lookahead bias on macro releases.

---

### Clarification: normalized_payload vs ingestion.normalized_events

`raw_source_records.normalized_payload` is a **connector-level structural mapping** — a direct field rename/type cast performed by the connector's `normalize()` method before writing to the DB. It contains only what can be derived from the raw payload without LLM inference (e.g. mapping `"close_price"` → `"close"`, converting string timestamps to ISO 8601).

`ingestion.normalized_events` is the **semantic normalization output** from the full NormalizationAgent (LLM-based NLP for sentiment, entities, topics, geography). Only text/event-based sources (news, GDELT) produce rows in this table. Price bar and macro sources do not.

### Version Chaining

When a source revises a previously published record (e.g. FRED revises a CPI figure), a new row is inserted with `record_version` incremented and `prior_record_id` set to the UUID of the previous version. This creates a linked list: `version 2 → version 1`. To find the current authoritative version for a given `(source_id, external_id)`, query the row with the highest `record_version`.

### Correlation ID

`predictions.predictions.correlation_id` is **NOT** independently generated per prediction. It must be set to the same UUID as the `ops.job_runs.correlation_id` for the pipeline run that generated the prediction. This allows filtering all log lines, model usage records, and audit log entries for a single prediction run with one correlation ID.

## Stage 2: Normalized Event

Text-based and event-based records (news, GDELT) are enriched and written to `ingestion.normalized_events`.

### NormalizedEvent

| Field | Type | Description |
|---|---|---|
| `source_record_id` | UUID | FK to the originating `raw_source_records` row |
| `event_type` | string | Category: `news`, `economic_release`, `geopolitical_event`, `corporate_filing` |
| `event_subtype` | string | More specific: `earnings_release`, `fed_speech`, `earthquake`, `trade_war` |
| `title` | string | Short title or headline |
| `summary` | string | One-paragraph summary (original or NLP-extracted) |
| `sentiment_score` | decimal | –1.0 (most negative) to +1.0 (most positive), 4 decimal places |
| `severity_score` | decimal | 0.0 (routine) to 1.0 (extreme), 4 decimal places |
| `country_code` | string | ISO 3166-1 alpha-2 code of primary geography |
| `region` | string | Sub-national region or broad region (e.g. `North America`) |
| `entity_data` | JSON | Extracted entities — see schema below |
| `event_occurred_at` | datetime (UTC) | When the real-world event occurred |

---

### entity_data JSONB Schema

```json
{
  "orgs":    [{"name": "Federal Reserve", "confidence": 0.97}],
  "persons": [{"name": "Jerome Powell",   "confidence": 0.91, "role": "Fed Chair"}],
  "assets":  [{"symbol": "BTC", "type": "crypto", "confidence": 0.99}],
  "places":  [{"name": "United States",  "country_code": "US", "confidence": 0.95}]
}
```

Each element contains `name` (string), `confidence` (0.0–1.0), and optional type-specific fields. Arrays may be empty; the top-level keys are always present.

---

## Stage 3: Feature Snapshot (Input to Prediction Engine)

The Prediction Engine receives a `FeatureSnapshot` containing the features available at the prediction issuance time. All feature timestamps must be strictly before `snapshot.as_of_at`.

### FeatureSnapshot

| Field | Type | Description |
|---|---|---|
| `snapshot_id` | UUID | FK to `features.feature_snapshots` |
| `asset_id` | UUID | The target asset for this snapshot |
| `asset_symbol` | string | Human-readable symbol (e.g. `BTC/USD`) |
| `as_of_at` | datetime (UTC) | The cutoff time — no features with data after this time are included |
| `feature_set_name` | string | The feature set version used |
| `values` | list[FeatureValue] | All computed feature values |

### FeatureValue

| Field | Type | Description |
|---|---|---|
| `feature_key` | string | Unique key within the snapshot (e.g. `price_return_24h`, `cpi_yoy_change`) |
| `feature_type` | string | `numeric`, `text`, `boolean`, `json` |
| `numeric_value` | decimal or null | Numeric value where applicable |
| `text_value` | string or null | Text value where applicable |
| `available_at` | datetime (UTC) | When this data was publicly available (must be < `as_of_at`) |
| `source_record_ids` | list[UUID] | Lineage back to `raw_source_records` rows |

### Example Snapshot (abbreviated)

```json
{
  "snapshot_id": "uuid",
  "asset_symbol": "BTC/USD",
  "as_of_at": "2026-04-18T14:00:00Z",
  "feature_set_name": "v1.0",
  "values": [
    {
      "feature_key": "price_return_1h",
      "feature_type": "numeric",
      "numeric_value": 0.0082,
      "available_at": "2026-04-18T13:59:59Z"
    },
    {
      "feature_key": "price_return_24h",
      "feature_type": "numeric",
      "numeric_value": -0.0231,
      "available_at": "2026-04-18T13:59:59Z"
    },
    {
      "feature_key": "news_sentiment_24h_avg",
      "feature_type": "numeric",
      "numeric_value": 0.1240,
      "available_at": "2026-04-18T13:45:00Z"
    },
    {
      "feature_key": "macro_cpi_yoy_latest",
      "feature_type": "numeric",
      "numeric_value": 3.2,
      "available_at": "2026-04-10T12:30:00Z"
    }
  ]
}
```

---

### direction_rule and settlement_rule Contract

`prediction_targets.direction_rule` and `settlement_rule` must be machine-parseable JSON objects, not free text. The evaluation engine deserializes these fields to determine settlement logic.

**direction_rule schema:**
```json
{
  "direction": "up",
  "metric":    "price_return",
  "threshold": 0.02,
  "unit":      "fraction"
}
```
- `direction`: `"up"` | `"down"` | `"neutral"`
- `metric`: `"price_return"` | `"absolute_price"` | `"relative_to_benchmark"`
- `threshold`: numeric (e.g. `0.02` = 2%); required for `up`/`down`; `null` for `neutral`
- `unit`: `"fraction"` | `"percent"` | `"usd"`

**settlement_rule schema:**
```json
{
  "type":    "trading_day_close",
  "horizon": "next_n_bars",
  "n":       1,
  "calendar": "NYSE"
}
```
- `type`: `"continuous"` (crypto 24/7) | `"trading_day_close"` (equity/commodity)
- `horizon`: `"next_n_bars"` | `"end_of_day"` | `"wall_clock_hours"`
- `n`: number of bars or hours (integer)
- `calendar`: `"NYSE"` | `"CME"` | `"LSE"` | `"none"` (for continuous markets)

These fields are stored as text in the DB currently; during implementation the column type should be changed to `jsonb` via a migration, or the JSON should be parsed on read by the application layer.

---

## Stage 4: Prediction Output

The final output of the Prediction Engine. Stored as an immutable row in `predictions.predictions`.

### Prediction

| Field | Type | Constraints | Description |
|---|---|---|---|
| `prediction_id` | UUID | PK | Stable identifier |
| `created_at` | datetime (UTC) | Not null | When the prediction was issued |
| `asset_symbol` | string | — | Human-readable asset (e.g. `BTC/USD`) |
| `asset_type` | string | — | `crypto`, `equity`, `commodity`, `forex` |
| `target_name` | string | — | Name of the `prediction_target` (e.g. `btc_up_2pct_24h`) |
| `target_metric` | string | — | What is being predicted (e.g. `price_return`) |
| `direction` | string | `up` \| `down` \| `neutral` | Predicted direction |
| `threshold` | decimal | — | Threshold used in the target definition (e.g. `0.02` for 2%) |
| `horizon_hours` | integer | > 0 | Forecast horizon in hours |
| `horizon_end_at` | datetime (UTC) | > created_at | When the prediction expires and must be evaluated |
| `probability` | decimal | 0.00–1.00, 2 dp display | Model's confidence the predicted outcome occurs |
| `prediction_mode` | string | `live` \| `backtest` | Live forward prediction or historical simulation |
| `evidence_summary` | string | ≤ 1000 chars | Human-readable rationale |
| `claim_type` | string | `correlation` \| `causal_hypothesis` | Epistemic label — never implies proven causation |
| `model_version` | string | — | Name + version of the model that generated this prediction |
| `feature_snapshot_id` | UUID | FK | The frozen feature set used at issuance time |

### Probability Format

- **Storage**: `NUMERIC(6,5)` in Postgres — full precision (e.g. `0.87300`)
- **Display / output**: always rounded to 2 decimal places (e.g. `0.87`)
- **Interpretation**: `0.87` means the model assigns an 87% probability to the predicted outcome
- **Valid range**: `0.00` to `1.00` inclusive (enforced by DB check constraint)
- **Confidence labels** (for UI display only):

| Range | Label |
|---|---|
| 0.00 – 0.39 | Low confidence |
| 0.40 – 0.59 | Moderate confidence |
| 0.60 – 0.74 | High confidence |
| 0.75 – 0.84 | Very high confidence |
| 0.85 – 1.00 | Alert threshold — triggers Telegram notification |

### Example Prediction

```json
{
  "prediction_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "created_at": "2026-04-18T14:01:00Z",
  "asset_symbol": "BTC/USD",
  "asset_type": "crypto",
  "target_name": "btc_up_2pct_24h",
  "target_metric": "price_return",
  "direction": "up",
  "threshold": 0.02,
  "horizon_hours": 24,
  "horizon_end_at": "2026-04-19T14:01:00Z",
  "probability": 0.87,
  "prediction_mode": "live",
  "evidence_summary": "24h price return is -2.3% suggesting mean reversion setup. News sentiment 24h average is mildly positive at 0.12. CPI YoY latest reading is 3.2% (released 2026-04-10), within recent range with no shock. Correlation-based signal only — no causal mechanism identified.",
  "claim_type": "correlation",
  "model_version": "heuristic-baseline-v1.0",
  "feature_snapshot_id": "uuid"
}
```

---

### rationale JSONB Schema

`predictions.predictions.rationale` stores structured metadata about the prediction generation process. Required keys:

```json
{
  "feature_count":          25,
  "features_omitted":       0,
  "context_compressed":     false,
  "compression_type":       null,
  "total_llm_cost_usd":     0.013251,
  "model_call_count":       3,
  "evidence_grounding_ok":  true,
  "claim_type":             "correlation",
  "self_consistency_runs":  null,
  "self_consistency_std":   null
}
```

Optional keys added when applicable:
- `"compression_type"`: `"soft"` | `"hard"` | `null`
- `"features_omitted"`: count of features dropped during compression
- `"self_consistency_runs"`: integer if self-consistency was enabled; null otherwise

### Alert Horizon Clarification

`ALERT_MAX_HORIZON_HOURS=72` means **72 wall-clock hours from prediction `created_at`**, not 3 calendar days. A prediction created at 23:00 Friday with a 72-hour horizon ends at 23:00 Monday. For equity predictions, this may cross weekends — the evaluation engine handles settlement using the trading calendar, but the alert gate uses wall-clock hours only.

---

## Stage 5: Alert Output (Telegram)

Sent when `probability >= 0.85` and `horizon_hours <= 72` (wall-clock hours) and `prediction_mode = 'live'` and `hallucination_risk = false` and `probability_extreme_flag = false`.

### AlertPayload

| Field | Type | Description |
|---|---|---|
| `prediction_id` | UUID | For lookup and idempotency |
| `created_at` | string | ISO 8601 UTC formatted timestamp |
| `asset_symbol` | string | e.g. `BTC/USD` |
| `direction` | string | `up` or `down` |
| `threshold_pct` | string | Human-readable threshold e.g. `> 2%` |
| `horizon_label` | string | Human-readable e.g. `within 24 hours` |
| `probability` | string | 2 decimal places e.g. `0.87` |
| `evidence_summary` | string | Truncated to 500 chars for alert readability |
| `claim_type_warning` | string | `Correlation only — causation not established` if claim_type is correlation |
| `model_version` | string | For operator reference |

### Example Telegram Alert Message

```
🔔 High-Confidence Prediction Alert

Asset:       BTC/USD
Direction:   UP > 2% within 24 hours
Probability: 0.87
Horizon end: 2026-04-19 14:01 UTC

Evidence:
24h price return is -2.3% suggesting mean reversion setup.
News sentiment 24h average is mildly positive at 0.12.
[Correlation only — causation not established]

Model:         heuristic-baseline-v1.0
Prediction ID: f47ac10b-...
```

---

## Evaluation Output

After `horizon_end_at`, the Evaluation Engine settles the prediction and writes:

| Field | Type | Description |
|---|---|---|
| `evaluation_state` | string | `evaluated`, `void`, `not_evaluable`, `superseded_target_definition` |
| `actual_outcome` | string | What actually happened (e.g. `up_3.1pct`) |
| `directional_correct` | boolean | Whether direction prediction was correct |
| `brier_score` | decimal | `(probability - outcome)^2`; 0.00 = perfect, 1.00 = worst |
| `return_pct` | decimal | Simulated paper return before costs |
| `cost_adjusted_return_pct` | decimal | Return after explicit cost assumption |
| `calibration_bucket` | string | 0.1-width bucket e.g. `0.80-0.90` |

### Brier Score Interpretation

| Score | Interpretation |
|---|---|
| 0.00 | Perfect prediction |
| 0.25 | No skill (equivalent to always predicting 0.5) |
| 0.50 – 1.00 | Worse than random |
