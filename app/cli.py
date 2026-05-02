from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.alerts.pipeline import AlertCheckPipeline
from app.config import settings
from app.connectors.alpha_vantage import AlphaVantageConnector
from app.connectors.cftc_cot import CftcCotConnector
from app.connectors.cboe_options import CboeOptionsConnector
from app.connectors.coingecko import CoinGeckoConnector
from app.connectors.eia import EiaConnector
from app.connectors.fear_greed import FearGreedConnector
from app.connectors.fred import FredConnector
from app.connectors.fred_calendar import FredCalendarConnector
from app.connectors.gdelt import GdeltConnector
from app.connectors.glasschain import GlasschainConnector
from app.connectors.newsapi import NewsApiConnector
from app.connectors.usgs import UsgsConnector
from app.db.pool import close_pool, init_pool
from app.db.seed import run_seed
from app.evaluation.pipeline import EvaluationPipeline
from app.features.pipeline import FeaturePipeline
from app.logging import configure_logging
from app.normalization import NormalizationPipeline
from app.ops.orchestrator import ResearchOrchestrator
from app.predictions.calibration import train_all_calibrators
from app.predictions.ensemble_engine import train_all_targets
from app.predictions.pipeline import PredictionPipeline

_log = structlog.get_logger(__name__)

StageRunner = Callable[[], Awaitable[None]]


_INGESTION_CONNECTORS = (
    ("CoinGecko", CoinGeckoConnector),
    ("FearGreed", FearGreedConnector),
    ("GDELT", GdeltConnector),
    ("USGS", UsgsConnector),
    ("FRED", FredConnector),
    ("FREDCalendar", FredCalendarConnector),
    ("EIA", EiaConnector),
    ("Glassnode", GlasschainConnector),
    ("CFTC COT", CftcCotConnector),
    ("Cboe Options", CboeOptionsConnector),
    ("NewsAPI", NewsApiConnector),
    ("AlphaVantage", AlphaVantageConnector),
)


async def run_ingestion() -> None:
    """Run all configured connectors sequentially. Errors per-connector are logged, not raised."""
    _log.info("ingestion_run_started", connectors=[name for name, _ in _INGESTION_CONNECTORS])
    succeeded = 0
    failed = 0
    for name, cls in _INGESTION_CONNECTORS:
        try:
            connector = cls()
            await connector.run()
            succeeded += 1
            _log.info("ingestion_connector_done", connector=name)
        except Exception as exc:  # noqa: BLE001 - keep iterating other connectors
            failed += 1
            _log.error("ingestion_connector_failed", connector=name, error=str(exc))
    _log.info("ingestion_run_complete", succeeded=succeeded, failed=failed)


def build_stage_registry() -> dict[str, StageRunner]:
    normalization = NormalizationPipeline()
    feature_pipeline = FeaturePipeline()
    prediction_pipeline = PredictionPipeline()
    alert_pipeline = AlertCheckPipeline()
    evaluation_pipeline = EvaluationPipeline()
    orchestrator = ResearchOrchestrator(
        ingestion_fn=run_ingestion,
        normalization_pipeline=normalization,
        feature_pipeline=feature_pipeline,
        prediction_pipeline=prediction_pipeline,
        alert_pipeline=alert_pipeline,
        evaluation_pipeline=evaluation_pipeline,
    )

    async def run_research_cycle() -> None:
        await orchestrator.run_cycle()

    return {
        "ingestion": run_ingestion,
        "normalization": normalization.run,
        "feature_generation": feature_pipeline.run,
        "prediction_run": prediction_pipeline.run,
        "alert_check": alert_pipeline.run,
        "evaluation": evaluation_pipeline.run,
        "research_cycle": run_research_cycle,
        "ensemble_train": train_all_targets,
        "calibration_train": train_all_calibrators,
    }


def build_scheduler_job_definitions(mode: str = "stages") -> list[dict]:
    if mode == "research-cycle":
        return [{"name": "research_cycle", "seconds": settings.cron_news_ingest_interval_seconds}]

    return [
        {"name": "ingestion", "seconds": settings.cron_news_ingest_interval_seconds},
        {"name": "normalization", "seconds": settings.cron_news_ingest_interval_seconds},
        {"name": "feature_generation", "seconds": settings.cron_news_ingest_interval_seconds},
        {"name": "prediction_run", "seconds": settings.cron_news_ingest_interval_seconds},
        {"name": "alert_check", "seconds": settings.cron_alert_check_interval_seconds},
        {"name": "evaluation", "seconds": settings.cron_evaluation_interval_seconds},
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prediction-app")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a single pipeline stage or the full research cycle")
    run_parser.add_argument(
        "stage",
        choices=sorted(build_stage_registry().keys()),
        help="Stage or orchestrated cycle to run",
    )

    schedule_parser = subparsers.add_parser("schedule", help="Start the APScheduler loop for the current pipelines")
    schedule_parser.add_argument(
        "--mode",
        choices=["stages", "research-cycle"],
        default="stages",
        help="Schedule individual stages or the combined research cycle",
    )
    return parser


async def run_named_stage(stage: str) -> None:
    registry = build_stage_registry()
    await registry[stage]()


async def run_scheduler(mode: str) -> None:
    registry = build_stage_registry()
    scheduler = AsyncIOScheduler()
    for job in build_scheduler_job_definitions(mode):
        scheduler.add_job(registry[job["name"]], "interval", seconds=job["seconds"], id=job["name"], replace_existing=True)

    scheduler.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        scheduler.shutdown(wait=False)


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging()
    await init_pool()
    await run_seed()
    try:
        if args.command == "run":
            await run_named_stage(args.stage)
        elif args.command == "schedule":
            await run_scheduler(args.mode)
        else:  # pragma: no cover
            parser.error(f"unsupported command: {args.command}")
        return 0
    finally:
        await close_pool()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))
