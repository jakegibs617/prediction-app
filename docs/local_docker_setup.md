# Local Docker Setup

This project can now run fully locally with Docker Compose.

## What it starts

- `db`: PostgreSQL with the schema bootstrapped from `sql/001_init_experimental_prediction_app.sql` and `sql/002_pending_migrations.sql`
- `app`: the prediction app container running `prediction-app schedule --mode stages`

The app uses the built-in APScheduler loop as the local cron replacement.

## First-time setup

1. Copy `.env.example` to `.env` if you have not already.
2. Make sure `POSTGRES_PASSWORD` in `.env` matches what you want for local Docker.
3. Start the stack:

```bash
docker compose up --build
```

## Useful commands

Run the full research cycle once:

```bash
docker compose run --rm app prediction-app run research_cycle
```

Run a single stage:

```bash
docker compose run --rm app prediction-app run feature_generation
docker compose run --rm app prediction-app run prediction_run
docker compose run --rm app prediction-app run alert_check
docker compose run --rm app prediction-app run evaluation
```

Start the scheduler in combined-cycle mode instead of per-stage mode:

```bash
docker compose run --rm app prediction-app schedule --mode research-cycle
```

Stop everything:

```bash
docker compose down
```

Reset the database volume completely:

```bash
docker compose down -v
```

## Interval configuration

The scheduler reads interval values from `.env` via these settings:

- `CRON_NEWS_INGEST_INTERVAL_SECONDS`
- `CRON_ALERT_CHECK_INTERVAL_SECONDS`
- `CRON_EVALUATION_INTERVAL_SECONDS`

For local testing, you can temporarily set them to smaller values like `30` or `60`.

## Notes

- The Docker app container forces `POSTGRES_HOST=db`, so it talks to the Compose Postgres service rather than your host database.
- Postgres init scripts only run on first volume creation. If you need a clean re-init after schema changes, use `docker compose down -v`.
