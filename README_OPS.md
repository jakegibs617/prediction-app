# Prediction App — Operations Guide

Automated probabilistic forecasting pipeline. Ingests market data + news, builds features, generates LLM-backed predictions, evaluates them against actual outcomes, and sends Telegram alerts.

> **Audience:** future OpenClaw sessions and the human operator. This is the runbook, not the design doc.

---

## At a glance

| Setting | Value |
|---|---|
| **App location** | `C:\Users\jakeg\OneDrive\Desktop\prediction-app` |
| **Python venv** | `.venv-win\` (Python 3.11) |
| **Database** | PostgreSQL 18 on `localhost:5432`, db `experimental_prediction_app`, user `prediction_app` |
| **LLM** | Ollama on `http://localhost:11434`, model `qwen3.5:latest` |
| **Daily schedule** | Windows Task Scheduler — `PredictionApp_DailyResearch` runs at **08:00 local** |
| **Alert threshold** | `min_probability = 0.65`, `max_horizon = 72h` |
| **Telegram chat** | `8510148267` (Jacob's account) |

---

## 1. Daily operation

### How it runs on its own
A Windows Scheduled Task fires `daily_research_cycle.bat` once a day at 08:00. The batch file calls `run_full_pipeline.py` and appends to `logs/daily_cycle.log`.

```powershell
# Inspect the schedule
schtasks /Query /TN "PredictionApp_DailyResearch" /V /FO LIST

# Run on demand (one-shot, exits when done)
schtasks /Run /TN "PredictionApp_DailyResearch"

# Disable / re-enable
schtasks /Change /TN "PredictionApp_DailyResearch" /DISABLE
schtasks /Change /TN "PredictionApp_DailyResearch" /ENABLE

# Delete entirely
schtasks /Delete /TN "PredictionApp_DailyResearch" /F
```

### Live tail of the log
```powershell
Get-Content "C:\Users\jakeg\OneDrive\Desktop\prediction-app\logs\daily_cycle.log" -Tail 40
```

---

## 2. Manually run any pipeline stage

> ⚠️ **All scripts must run from the prediction-app directory** so pydantic-settings finds `.env`. The provided scripts already `os.chdir(...)` at the top, but if writing new ones, always do that first.

```powershell
cd "C:\Users\jakeg\OneDrive\Desktop\prediction-app"

# Full pipeline (feature_generation -> prediction_run -> alert_check -> evaluation)
& ".\.venv-win\Scripts\python.exe" run_full_pipeline.py

# Just normalization (raw -> normalized_events; LLM extraction)
& ".\.venv-win\Scripts\python.exe" run_normalization_clean.py
```

The CLI also exposes individual stages, but it has Windows path-import quirks. Prefer the scripts above unless you know what you're doing.

---

## 3. Inspect the database

```powershell
cd "C:\Users\jakeg\OneDrive\Desktop\prediction-app"

# Counts at every stage (raw -> norm -> features -> predictions -> evals -> alerts)
& ".\.venv-win\Scripts\python.exe" run_normalization_clean.py   # also prints counts
& ".\.venv-win\Scripts\python.exe" check_validation.py          # raw record statuses

# Diagnostic: what targets/assets exist? Snapshots & price bars per asset?
& ".\.venv-win\Scripts\python.exe" diagnose_predictions.py

# Alert deliveries
& ".\.venv-win\Scripts\python.exe" check_deliveries.py

# Inspect the active alert rule (threshold, destination)
& ".\.venv-win\Scripts\python.exe" inspect_alert_rules.py
```

### Schema reminder
- `ingestion.raw_source_records` — every record fetched from external APIs (1137+ rows)
- `ingestion.normalized_events` — LLM-extracted news/event metadata (160+ rows)
- `market_data.assets` — 11 active (BTC, ETH, XRP, SOL, AVAX, SPY, QQQ, GLD, SLV, USO, TLT)
- `market_data.price_bars` — daily close bars per asset
- `features.feature_snapshots` — point-in-time features (RSI, momentum, vol, etc.)
- `predictions.prediction_targets` — 6 active targets across crypto / equity / commodity
- `predictions.predictions` — actual probabilistic predictions
- `evaluation.evaluation_results` — scored once horizon elapses
- `ops.alert_rules` — alert thresholds (`min_probability`, `max_horizon_hours`, `destination`)
- `ops.alert_deliveries` — Telegram delivery log (status, attempts, last_error)
- `ops.job_runs` — pipeline lock table

### Active prediction targets
```
crypto:    BTC/USD up   >2%  in 24h
crypto:    ETH/USD down >3%  in 48h
equity:    SPY positive next trading day
equity:    QQQ positive next trading day
commodity: GLD up >1.5% in 48h
commodity: USO up >2%   in 48h
```

---

## 4. Tuning

### Change the alert threshold
The threshold lives in **two** places — both must be updated:

1. `.env` → `ALERT_MIN_PROBABILITY=0.65`
2. `ops.alert_rules` table — the per-rule value is what actually gates delivery.

Example: bump to 0.85 for production-grade noise control.

```powershell
# .env: edit ALERT_MIN_PROBABILITY
# Then update the DB rule (creates a tiny inline script):
cd "C:\Users\jakeg\OneDrive\Desktop\prediction-app"
@"
import asyncio, os, sys
os.chdir(r'C:\Users\jakeg\OneDrive\Desktop\prediction-app')
sys.path.insert(0, r'C:\Users\jakeg\OneDrive\Desktop\prediction-app')
from app.db.pool import close_pool, get_pool, init_pool

async def main():
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE ops.alert_rules SET min_probability = 0.85 WHERE name = \$1',
            'default_telegram_high_confidence',
        )
    await close_pool()

asyncio.run(main())
"@ | Out-File _tmp_threshold.py -Encoding utf8
& ".\.venv-win\Scripts\python.exe" _tmp_threshold.py
Remove-Item _tmp_threshold.py
```

### Change the model
- Edit `.env` → `AI_MODEL_NAME` and `AI_MODEL_NAME_CHEAP`
- Make sure the model is pulled in Ollama: `ollama list`. If missing: `ollama pull <name>`
- Models known to work: `qwen3.5:latest` (current — best grounding), `mistral:7b` (faster, lower quality)
- Keep `AI_MODEL_PROVIDER=ollama` unless wiring up Groq/OpenAI/Anthropic

> Note: `OllamaClient` already passes `think: false` so reasoning models (qwen3.x) skip chain-of-thought. Without it they burn the entire token budget on hidden thinking and return empty responses.

### Add a new prediction target
See `seed_targets.py` and `reclassify_and_seed_etf_targets.py` for the pattern. Required fields:
- `asset_type` must be one of `crypto`, `equity`, `commodity`, `forex` (NOT `etf` — schema constraint; SPY/QQQ/TLT are stored as `equity`, GLD/SLV/USO as `commodity`).
- `direction_rule` is JSON — see contracts in `app/predictions/contracts.py`.
- `settlement_rule` ditto. Use `type=continuous + horizon=wall_clock_hours` for crypto, `type=trading_day_close + calendar=NYSE` for equities.

---

## 5. Common operations

### Reset stuck job locks
If the logs say `job_already_running` but no logs are streaming, a previous run died without releasing the lock. Clear it:

```powershell
& ".\.venv-win\Scripts\python.exe" kill_normalization.py
```

This sets all `running` rows in `ops.job_runs` to `failed`. Retries are then unblocked. (Despite the script name, this works for any stale job.)

### Re-process quarantined records
If validation_status=quarantined records pile up after a model regression:

```powershell
& ".\.venv-win\Scripts\python.exe" reset_all_quarantined.py
& ".\.venv-win\Scripts\python.exe" run_normalization_clean.py
```

### Fully refresh predictions today
The pipeline naturally creates a new prediction per (target, latest_feature_snapshot) and skips existing ones. To force a full re-run, just run feature_generation first to mint fresh snapshots, then prediction_run.

```powershell
& ".\.venv-win\Scripts\python.exe" run_full_pipeline.py
```

### Stop / start Postgres or Ollama
```powershell
# Postgres (Windows service)
Stop-Service postgresql-x64-18    # may need admin
Start-Service postgresql-x64-18

# Ollama runs as user-level processes; check with:
Get-Process | Where-Object { $_.ProcessName -like '*ollama*' }
# To start Ollama explicitly: open the Ollama tray app, or run `ollama serve` in a shell.
```

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `job_already_running` but no progress | Stale lock from killed run | `kill_normalization.py` |
| `LOG_FILE_PATH=./logs/app.log` resolved relative | Script not run from app directory | `cd` first or use the provided scripts |
| `password authentication failed for user "postgres"` | `pg_hba.conf` reverted or wrong | Local connections should be `trust`. See `pg_hba.conf` (PG18 data dir). |
| Ollama 404 on `/v1/chat/completions` | Wrong endpoint (this Ollama doesn't expose OpenAI-compat) | We use `/api/generate` directly via `OllamaClient`. Don't change. |
| Predictions all `hallucination_risk=True` | Grounding check too strict | See `_check_evidence_grounding` in `app/predictions/llm_engine.py`. Already loosened to tolerate decimal/percent variants and target/event numbers. |
| qwen3.5 returns empty `response: ""` | Reasoning model burned tokens on hidden thinking | `OllamaClient` already sets `think: false`. Don't remove. |
| Daily task says `Last Result: 267009` but log empty | Task launched but Python crashed before redirecting | Check `logs/daily_cycle.log` and Windows Event Viewer → Task Scheduler. |
| No Telegram messages but `delivery_status=sent` | Bot has never had `/start` from your account | Open Telegram, search bot by token, send `/start`. |

---

## 7. Phase 2: Docker (deferred)

Installer present at `C:\Users\jakeg\Downloads\Docker Desktop Installer.exe`. The app already has `docker-compose.yml` and `Dockerfile` — once Docker is installed, `docker compose up` should bring up Postgres + the app stack. Ollama would still run on the host (it's not in the compose file).

This is **not required** for any current functionality — the daily scheduler runs everything natively on Windows. Docker is for portability (other machines, VPS, etc.).

---

## 8. Files of interest

```
prediction-app/
├── app/                              ← the actual app
│   ├── normalization/
│   │   ├── pipeline.py               ← raw → normalized_events
│   │   └── contracts.py              ← Pydantic shape with tolerance for local LLMs
│   ├── features/pipeline.py          ← market_data → feature_snapshots
│   ├── predictions/
│   │   ├── pipeline.py               ← orchestrator
│   │   ├── service.py                ← candidate selection
│   │   └── llm_engine.py             ← LLM call + hallucination guard
│   ├── alerts/
│   │   ├── pipeline.py               ← reads predictions, applies rules
│   │   └── service.py                ← Telegram delivery
│   ├── evaluation/pipeline.py        ← scores predictions whose horizon has elapsed
│   ├── model_client/
│   │   ├── ollama.py                 ← native /api/generate, think:false, format:json
│   │   ├── _openai_compat.py         ← OpenAI/Groq/Anthropic OpenAI-compat path
│   │   └── factory.py                ← provider routing
│   └── config.py                     ← Pydantic Settings (reads .env)
├── sql/
│   └── 001_init_experimental_prediction_app.sql ← full schema
├── .env                              ← all secrets + config
├── run_full_pipeline.py              ← USE THIS for a full pass
├── run_normalization_clean.py        ← USE THIS for normalization only
├── daily_research_cycle.bat          ← what Task Scheduler runs
├── logs/daily_cycle.log              ← scheduler output
├── README_OPS.md                     ← this file
└── (helper scripts: seed_targets, reset_*, check_*, diagnose_*, kill_*, ...)
```

---

## 9. Tests

```powershell
cd "C:\Users\jakeg\OneDrive\Desktop\prediction-app"

# Quick run (1 sec)
& ".\.venv-win\Scripts\python.exe" -m pytest tests/ -q

# With coverage report
& ".\.venv-win\Scripts\python.exe" -m pytest tests/ --cov=app --cov-report=term-missing -q

# Single file
& ".\.venv-win\Scripts\python.exe" -m pytest tests/test_alert_rules.py -v
```

**Current state (2026-04-25):** 66 tests, all passing, ~44% line coverage (high coverage on pipeline logic; 0% on API connectors and LLM HTTP clients which are intentionally not unit-tested).

If the dev deps aren't installed:
```powershell
& ".\.venv-win\Scripts\python.exe" -m pip install "pytest>=8.2.0" "pytest-asyncio>=0.23.6" "pytest-cov>=5.0.0"
```

---

## 10. Daily-use cheat sheet

```powershell
# I want to run the pipeline now
schtasks /Run /TN "PredictionApp_DailyResearch"

# I want to see the latest log
Get-Content "C:\Users\jakeg\OneDrive\Desktop\prediction-app\logs\daily_cycle.log" -Tail 40

# I want to see counts
cd "C:\Users\jakeg\OneDrive\Desktop\prediction-app"
& ".\.venv-win\Scripts\python.exe" run_normalization_clean.py

# I want to see what alerts went out
& ".\.venv-win\Scripts\python.exe" check_deliveries.py
```

That's the whole story.
