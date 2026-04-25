from __future__ import annotations

from uuid import uuid4

import pytest

from app.features.pipeline import FeaturePipeline
from app.features.service import FeatureCandidate


@pytest.mark.asyncio
async def test_feature_pipeline_runs_candidates(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_acquire(*args, **kwargs):
        calls.append("acquire")
        return uuid4()

    async def fake_increment(*args, **kwargs):
        calls.append("increment")
        return 1

    async def fake_release(*args, **kwargs):
        calls.append("release")
        return None

    async def fake_dead_letter(*args, **kwargs):
        calls.append("dead_letter")
        return None

    async def fake_read_candidates():
        return [FeatureCandidate(asset_id=uuid4(), asset_symbol="BTC/USD")]

    async def fake_generate(candidate):
        calls.append("generate")
        return object()

    monkeypatch.setattr("app.features.pipeline.acquire_job_lock", fake_acquire)
    monkeypatch.setattr("app.features.pipeline.increment_attempt", fake_increment)
    monkeypatch.setattr("app.features.pipeline.release_job_lock", fake_release)
    monkeypatch.setattr("app.features.pipeline.send_to_dead_letter", fake_dead_letter)
    monkeypatch.setattr("app.features.pipeline.read_feature_candidates", fake_read_candidates)
    monkeypatch.setattr("app.features.pipeline.generate_features_for_asset", fake_generate)

    await FeaturePipeline().run()

    assert calls[0:2] == ["acquire", "increment"]
    assert "generate" in calls
    assert calls[-1] == "release"
