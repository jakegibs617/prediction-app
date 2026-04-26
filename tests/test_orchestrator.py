from __future__ import annotations

import pytest

from app.ops.orchestrator import ResearchOrchestrator


class StubPipeline:
    def __init__(self, name: str, calls: list[str], state: dict | None = None) -> None:
        self.name = name
        self.calls = calls
        self.state = state if state is not None else {}

    async def run(self) -> None:
        self.calls.append(self.name)
        if self.name == "feature":
            self.state["features_created"] = True
        elif self.name == "prediction":
            self.state["prediction_created"] = True
        elif self.name == "alert":
            self.state["alert_sent"] = self.state.get("prediction_created", False)
        elif self.name == "evaluation":
            self.state["evaluated"] = self.state.get("prediction_created", False)


@pytest.mark.asyncio
async def test_research_orchestrator_runs_stages_in_order() -> None:
    calls: list[str] = []
    orchestrator = ResearchOrchestrator(
        normalization_pipeline=StubPipeline("normalization", calls),
        feature_pipeline=StubPipeline("feature", calls),
        prediction_pipeline=StubPipeline("prediction", calls),
        alert_pipeline=StubPipeline("alert", calls),
        evaluation_pipeline=StubPipeline("evaluation", calls),
    )

    result = await orchestrator.run_cycle()

    assert calls == ["normalization", "feature", "prediction", "alert", "evaluation"]
    assert result.normalization_ran
    assert result.feature_generation_ran
    assert result.prediction_ran
    assert result.alerting_ran
    assert result.evaluation_ran


@pytest.mark.asyncio
async def test_research_orchestrator_smoke_lifecycle() -> None:
    calls: list[str] = []
    state: dict[str, bool] = {}
    orchestrator = ResearchOrchestrator(
        normalization_pipeline=StubPipeline("normalization", calls, state),
        feature_pipeline=StubPipeline("feature", calls, state),
        prediction_pipeline=StubPipeline("prediction", calls, state),
        alert_pipeline=StubPipeline("alert", calls, state),
        evaluation_pipeline=StubPipeline("evaluation", calls, state),
    )

    await orchestrator.run_cycle()

    assert state == {
        "features_created": True,
        "prediction_created": True,
        "alert_sent": True,
        "evaluated": True,
    }
