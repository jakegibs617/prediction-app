# Admin UI

## Purpose

The Admin UI gives operators a browser-based control panel to adjust configurable levers, manage data sources, monitor pipeline health, and review predictions — without requiring direct database access or code changes.

All changes made through the Admin UI are logged to `ops.audit_logs` with the operator's identity, the before/after values, and a timestamp.

---

## Tech Stack

**MVP recommendation: FastAPI + Jinja2 + HTMX**

- Served from the existing FastAPI application under the `/admin` route prefix.
- Jinja2 templates for server-rendered HTML — no separate frontend build step.
- HTMX for inline updates (toggle a source on/off, save a setting) without full page reloads.
- No JavaScript framework required at MVP. Can be replaced with a React or Vue frontend later by converting the same FastAPI routes to JSON API endpoints.

**Why not React for MVP**: a separate frontend doubles build/deploy complexity. HTMX gives interactive UX from plain HTML templates and can be stripped out when a richer frontend is warranted.

---

## Authentication

All `/admin` routes require authentication. Two options depending on deployment stage:

| Stage | Auth method |
|---|---|
| Local dev | Single shared admin password via HTTP Basic Auth (`ADMIN_PASSWORD` env var) |
| Staging / Production | Session-based login with role-scoped tokens |

Admin actions are **role-scoped**:

| Role | Access |
|---|---|
| `viewer` | Read-only: dashboard, predictions, audit log |
| `operator` | Viewer + enable/disable sources, re-queue dead-letter jobs, adjust alert thresholds |
| `admin` | Operator + add/remove sources, change model config, change pipeline safety limits, manage users |

Secrets (API keys, bot tokens) are **never displayed** in the UI after initial save — only a masked preview (e.g. `1234...abcd`) is shown. Editing requires re-entering the full value.

---

## Pages and Configurable Levers

### 1. Dashboard

Read-only health overview. Loaded at `/admin`.

| Section | What it shows |
|---|---|
| Pipeline status | Last run time and status for each cron stage (ingestion, normalization, feature generation, prediction, evaluation, alerting) |
| Source freshness | For each active source: last successful fetch, records ingested in last 24h, current error rate |
| Prediction summary | Counts: live predictions issued today, evaluated today, pending evaluation, voided |
| Recent alerts | Last 10 Telegram alerts sent, with prediction ID, asset, and probability |
| Dead-letter queue | Count of jobs in `dead_letter` status; link to Dead Letter page |

---

### 2. Sources

Manage `ops.api_sources`. Loaded at `/admin/sources`.

**Configurable fields per source:**

| Field | Type | Editable | Description |
|---|---|---|---|
| `name` | string | No (set at creation) | Unique source identifier |
| `category` | string | Yes | `events`, `news`, `macro`, `market_data` |
| `base_url` | string | Yes | API base URL |
| `auth_type` | string | Yes | `none`, `api_key`, `bearer`, `basic` |
| `api_key` | secret | Yes (write-only display) | API key — stored encrypted; never shown after save |
| `trust_level` | enum | Yes (admin only) | `verified`, `unverified`, `quarantine` |
| `is_active` | boolean | Yes | Toggle to pause/resume ingestion |
| `rate_limit_per_minute` | integer | Yes | Request budget enforced by connector |
| `max_attempts` | integer | Yes | Override default retry attempts for this source |
| `ingest_cron_interval_seconds` | integer | Yes | Override the global cron interval for this source |
| `notes` | text | Yes | Free-text operator notes (legal caveats, known quirks) |

**Actions available:**

- **Add source** — form with all fields above; new sources default to `trust_level = unverified` and `is_active = false` until manually enabled.
- **Enable / Disable** — inline toggle; logs to `ops.audit_logs`.
- **Re-run now** — trigger an immediate ingestion job for this source outside the cron schedule.
- **View health** — last 10 job runs for this source, with status, duration, and error detail.
- **Set trust level** — admin-only; promotes a source from `unverified` to `verified` or sends to `quarantine`.

---

### 3. Pipeline Settings

Adjust global processing levers. Loaded at `/admin/settings/pipeline`.

All changes take effect on the next job run. Changes are written to a `ops.config_overrides` table (see schema additions below) so they survive restarts without requiring `.env` edits.

| Setting | Default | Description |
|---|---|---|
| `MAX_AGENT_TOOL_CALLS` | 20 | Max tool calls per single LLM agent invocation before loop guard triggers |
| `MAX_AGENT_ITERATIONS` | 5 | Max reasoning iterations (self-analysis passes) the prediction agent may take per prediction |
| `MAX_AGENT_INPUT_TOKENS` | 6000 | Max tokens fed to the model per call |
| `MAX_AGENT_OUTPUT_TOKENS` | 2000 | Max tokens the model may generate per call |
| `MAX_FEATURES_PER_PREDICTION` | 25 | Features passed to the prediction model per snapshot |
| `MAX_EVIDENCE_SUMMARY_CHARS` | 1000 | Max length of evidence summary text |
| `JOB_MAX_RUNTIME_SECONDS` | 300 | Global job timeout; sources can override |
| `DEFAULT_JOB_MAX_ATTEMPTS` | 3 | Retry budget for failed jobs before dead-letter |

Each field includes:
- Current value and the last-changed timestamp.
- The default value (for one-click reset).
- A brief description of what the lever controls.
- Validation: numeric range enforced server-side (e.g. `MAX_AGENT_TOOL_CALLS` must be 1–100).

---

### 4. Model Settings

Switch AI model provider and model name. Loaded at `/admin/settings/model`.

| Setting | Default | Options |
|---|---|---|
| `AI_MODEL_PROVIDER` | `ollama` | `ollama`, `anthropic`, `openai`, `groq` |
| `AI_MODEL_NAME` | `llama3.2:8b` | Free-text (validated against known model names for the selected provider) |
| `MAX_AGENT_INPUT_TOKENS` | 6000 | Numeric |
| `MAX_AGENT_OUTPUT_TOKENS` | 2000 | Numeric |

API keys for non-Ollama providers are entered here (write-only, masked after save).

A **Test Connection** button sends a minimal prompt to the configured provider and reports latency and success/failure before saving — so misconfigurations are caught before they break a prediction run.

---

### 5. Alert Rules

Adjust alerting thresholds and destinations. Loaded at `/admin/settings/alerts`.

Backed by `ops.alert_rules` — changes here update the database row directly.

| Setting | Default | Description |
|---|---|---|
| `min_probability` | 0.85 | Minimum probability to trigger a Telegram alert |
| `max_horizon_hours` | 72 | Maximum horizon (hours) for an alertable prediction |
| `channel_type` | `telegram` | Alert destination type |
| `destination` (chat ID) | — | Telegram chat ID or channel target |
| `is_active` | true | Toggle alerting on/off globally |

A **Send Test Alert** button sends a mock prediction message to the configured Telegram destination to verify delivery without waiting for a real prediction.

---

### 6. Prediction Targets

View and manage `predictions.prediction_targets`. Loaded at `/admin/targets`.

| Field | Editable | Description |
|---|---|---|
| `name` | No (set at creation) | Unique target identifier |
| `asset_type` | Yes | `crypto`, `equity`, `commodity`, `forex` |
| `target_metric` | Yes | What is measured (e.g. `price_return`) |
| `direction_rule` | Yes | Condition text (e.g. `up > 2%`) |
| `horizon_hours` | Yes | Forecast horizon in hours |
| `settlement_rule` | Yes | `continuous` (crypto) or `trading_day_close` (equity) |
| `is_active` | Yes | Toggle to include/exclude from prediction runs |

**Add target** form enforces required fields and validates `horizon_hours > 0`.

Deactivating a target does not void existing predictions — it only stops new predictions from being generated for that target.

---

### 7. Dead Letter Queue

Inspect and requeue stuck jobs. Loaded at `/admin/dead-letter`.

Shows all `ops.job_runs` rows with `status = 'dead_letter'`, grouped by `job_name`.

Per-row actions:
- **Re-queue** — reset `status` to `queued` and `attempt_count` to `0`; logs the re-queue action.
- **Dismiss** — mark as acknowledged without re-running; logs the dismissal with a required reason field.
- **View logs** — filter `./logs/app.log` by `correlation_id` to show all log lines for this job run.

---

### 8. Audit Log

Read-only view of `ops.audit_logs`. Loaded at `/admin/audit`.

Filterable by: `entity_type`, `action`, `correlation_id`, date range.

---

## Schema Additions Required

The following columns and table are needed to support the Admin UI and must be added via migration before building the UI.

```sql
-- Migration: add trust_level to api_sources
ALTER TABLE ops.api_sources
    ADD COLUMN trust_level text NOT NULL DEFAULT 'unverified',
    ADD COLUMN rate_limit_per_minute integer,
    ADD COLUMN notes text,
    ADD CONSTRAINT api_sources_trust_level_check
        CHECK (trust_level IN ('verified', 'unverified', 'quarantine'));

-- Migration: persistent config overrides table
-- Stores admin UI changes to pipeline settings, superseding .env defaults at runtime.
CREATE TABLE ops.config_overrides (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key text NOT NULL UNIQUE,
    value text NOT NULL,
    default_value text NOT NULL,
    description text,
    updated_by text NOT NULL DEFAULT 'system',
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

---

## Config Override Resolution Order

At runtime, each setting resolves in this priority order (highest wins):

```
ops.config_overrides (DB)  >  .env file  >  coded default
```

This means an operator can override any setting through the Admin UI without touching files. Removing a row from `ops.config_overrides` falls back to the `.env` value.

---

## API Endpoints (FastAPI Routes)

All admin routes are under `/admin`. JSON API variants are under `/api/admin` for future frontend decoupling.

| Method | Path | Description |
|---|---|---|
| GET | `/admin` | Dashboard |
| GET | `/admin/sources` | List all sources |
| POST | `/admin/sources` | Add a new source |
| PATCH | `/admin/sources/{id}` | Update source fields |
| POST | `/admin/sources/{id}/toggle` | Enable/disable a source |
| POST | `/admin/sources/{id}/run-now` | Trigger immediate ingestion |
| GET | `/admin/settings/pipeline` | View pipeline settings |
| PATCH | `/admin/settings/pipeline` | Update pipeline settings |
| GET | `/admin/settings/model` | View model settings |
| PATCH | `/admin/settings/model` | Update model settings |
| POST | `/admin/settings/model/test` | Test model connection |
| GET | `/admin/settings/alerts` | View alert rules |
| PATCH | `/admin/settings/alerts` | Update alert rules |
| POST | `/admin/settings/alerts/test` | Send test alert |
| GET | `/admin/targets` | List prediction targets |
| POST | `/admin/targets` | Add a prediction target |
| PATCH | `/admin/targets/{id}` | Update a target |
| GET | `/admin/dead-letter` | View dead-letter jobs |
| POST | `/admin/dead-letter/{id}/requeue` | Re-queue a dead-letter job |
| POST | `/admin/dead-letter/{id}/dismiss` | Dismiss without re-queuing |
| GET | `/admin/audit` | View audit log |
