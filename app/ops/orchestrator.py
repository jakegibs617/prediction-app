from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from app.alerts.pipeline import AlertCheckPipeline
from app.evaluation.pipeline import EvaluationPipeline
from app.features.pipeline import FeaturePipeline
from app.normalization.pipeline import NormalizationPipeline
from app.predictions.pipeline import PredictionPipeline

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ResearchCycleResult:
    ingestion_ran: bool
    normalization_ran: bool
    feature_generation_ran: bool
    prediction_ran: bool
    alerting_ran: bool
    evaluation_ran: bool


class ResearchOrchestrator:
    def __init__(
        self,
        *,
        ingestion_fn: Callable[[], Awaitable[None]] | None = None,
        normalization_pipeline: NormalizationPipeline | None = None,
        feature_pipeline: FeaturePipeline | None = None,
        prediction_pipeline: PredictionPipeline | None = None,
        alert_pipeline: AlertCheckPipeline | None = None,
        evaluation_pipeline: EvaluationPipeline | None = None,
    ) -> None:
        self.ingestion_fn = ingestion_fn
        self.normalization_pipeline = normalization_pipeline or NormalizationPipeline()
        self.feature_pipeline = feature_pipeline or FeaturePipeline()
        self.prediction_pipeline = prediction_pipeline or PredictionPipeline()
        self.alert_pipeline = alert_pipeline or AlertCheckPipeline()
        self.evaluation_pipeline = evaluation_pipeline or EvaluationPipeline()

    async def run_cycle(self) -> ResearchCycleResult:
        log.info("research_cycle_started")

        if self.ingestion_fn is not None:
            await self.ingestion_fn()
        await self.normalization_pipeline.run()
        await self.feature_pipeline.run()
        await self.prediction_pipeline.run()
        await self.alert_pipeline.run()
        await self.evaluation_pipeline.run()

        result = ResearchCycleResult(
            ingestion_ran=self.ingestion_fn is not None,
            normalization_ran=True,
            feature_generation_ran=True,
            prediction_ran=True,
            alerting_ran=True,
            evaluation_ran=True,
        )
        log.info("research_cycle_completed", **result.__dict__)
        return result
