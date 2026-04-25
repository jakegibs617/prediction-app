# prediction-app

Automated probabilistic forecasting pipeline. Ingests market data and news, builds features, generates LLM-backed predictions, evaluates them against actual outcomes, and sends Telegram alerts.

## Stack

- **Python 3.11** (`asyncio`, `asyncpg`, `pydantic`, `structlog`)
- **PostgreSQL** for ingestion / features / predictions / evaluation / ops schemas
- **Ollama** (local) — provider-agnostic `ModelClient` also supports Groq / OpenAI / Anthropic
- **APScheduler** for cron, Windows Task Scheduler for the daily one-shot
- **Telegram Bot API** for alert delivery
- **pytest** for unit tests (66 passing, ~44% line coverage on tested modules)

## Pipeline stages

```
raw_source_records  --(normalize)-->  normalized_events
price_bars          --(features)-->   feature_snapshots
                    --(predict)-->    predictions       --(alert if prob >= threshold)--> Telegram
                    --(evaluate)-->   evaluation_results (after horizon elapses)
```

## Quick start (local)

```bash
# 1. Postgres up locally with the database experimental_prediction_app
psql -U postgres -f sql/001_init_experimental_prediction_app.sql

# 2. Configure environment (copy and fill in API keys)
cp .env.example .env
# edit .env: ALPHA_VANTAGE_API_KEY, NEWS_API_KEY, FRED_API_KEY, GROQ_API_KEY,
#            TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 3. Python deps
python -m venv .venv-win
.venv-win/Scripts/python -m pip install -e ".[dev]"

# 4. Run a full research cycle
.venv-win/Scripts/python run_full_pipeline.py

# 5. Run the unit tests
.venv-win/Scripts/python -m pytest tests/ -q
```

## Documentation

- **[`README_OPS.md`](README_OPS.md)** — full operations runbook: daily ops, manual stages, DB inspection, tuning, troubleshooting, schema, and a daily-use cheat sheet.
- **`progress.json`** — design notes and remaining roadmap items.
- **`docs/`** — additional design docs.

## License

(none specified)
