"""Unit tests for the statistical ensemble engine."""
from __future__ import annotations

import pickle
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.predictions.ensemble_engine import (
    ENSEMBLE_WEIGHT,
    LLM_WEIGHT,
    MIN_TRAINING_SAMPLES,
    _predict_probability,
    blend_probabilities,
    load_ensemble_model,
    maybe_blend_with_ensemble,
)
from app.predictions.contracts import (
    DirectionRule,
    FeatureSnapshot,
    FeatureValue,
    PredictionInput,
    PredictionTarget,
    SettlementRule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_feature_value(key: str, value: float) -> FeatureValue:
    return FeatureValue(
        feature_key=key,
        feature_type="numeric",
        numeric_value=value,
        available_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _make_snapshot(features: dict[str, float]) -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_id=uuid4(),
        asset_id=uuid4(),
        asset_symbol="BTC/USD",
        as_of_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        feature_set_name="price-baseline-v1",
        values=[_make_feature_value(k, v) for k, v in features.items()],
    )


def _make_prediction_input(snapshot: FeatureSnapshot, probability: float = 0.7) -> PredictionInput:
    target = PredictionTarget(
        id=uuid4(),
        name="BTC 24h up >2%",
        asset_type="crypto",
        target_metric="price_return",
        horizon_hours=24,
        direction_rule=DirectionRule(direction="up", metric="price_return", threshold=0.02, unit="fraction"),
        settlement_rule=SettlementRule(type="continuous", horizon="wall_clock_hours", n=24, calendar="none"),
        asset_id=snapshot.asset_id,
    )
    return PredictionInput(
        target=target,
        asset_id=snapshot.asset_id,
        asset_symbol=snapshot.asset_symbol,
        asset_type="crypto",
        feature_snapshot=snapshot,
        model_version_id=uuid4(),
        probability=probability,
        evidence_summary="Price returned 2% in 24h. Mean reversion signal present.",
        predicted_outcome="up_2pct",
        prediction_mode="live",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        correlation_id=uuid4(),
        rationale={"feature_count": len(snapshot.values), "model_name": "llm-prediction-engine"},
    )


def _make_fitted_pipeline(feature_names: list[str]) -> Pipeline:
    """Return a fitted pipeline on minimal synthetic data."""
    rng = np.random.default_rng(42)
    X = rng.standard_normal((60, len(feature_names)))
    y = (X[:, 0] > 0).astype(int)
    pipeline = Pipeline([("scaler", StandardScaler()), ("clf", LogisticRegression(max_iter=200, random_state=42))])
    pipeline.fit(X, y)
    return pipeline


# ---------------------------------------------------------------------------
# blend_probabilities
# ---------------------------------------------------------------------------


def test_blend_is_weighted_average() -> None:
    result = blend_probabilities(0.8, 0.6)
    expected = round(LLM_WEIGHT * 0.8 + ENSEMBLE_WEIGHT * 0.6, 5)
    assert abs(result - expected) < 1e-9


def test_blend_clamps_to_0_05_lower() -> None:
    assert blend_probabilities(0.05, 0.05) == 0.05


def test_blend_clamps_to_0_95_upper() -> None:
    assert blend_probabilities(0.95, 0.95) == 0.95


def test_blend_symmetric_at_0_5() -> None:
    assert blend_probabilities(0.5, 0.5) == 0.5


# ---------------------------------------------------------------------------
# _predict_probability
# ---------------------------------------------------------------------------


def test_predict_probability_returns_float_in_range() -> None:
    feature_names = ["price_return_24h", "price_return_1h"]
    pipeline = _make_fitted_pipeline(feature_names)
    snapshot = _make_snapshot({"price_return_24h": 0.02, "price_return_1h": 0.005})
    result = _predict_probability(pipeline, feature_names, snapshot)
    assert result is not None
    assert 0.05 <= result <= 0.95


def test_predict_probability_uses_zero_for_missing_features() -> None:
    feature_names = ["price_return_24h", "rsi_14", "volume_ratio"]
    pipeline = _make_fitted_pipeline(feature_names)
    # snapshot only has price_return_24h; others should be imputed to 0
    snapshot = _make_snapshot({"price_return_24h": 0.01})
    result = _predict_probability(pipeline, feature_names, snapshot)
    assert result is not None


def test_predict_probability_clamps_extremes() -> None:
    feature_names = ["x"]
    pipeline = _make_fitted_pipeline(feature_names)
    snapshot = _make_snapshot({"x": 1e9})
    result = _predict_probability(pipeline, feature_names, snapshot)
    assert result is not None
    assert 0.05 <= result <= 0.95


# ---------------------------------------------------------------------------
# load_ensemble_model
# ---------------------------------------------------------------------------


def test_load_ensemble_model_returns_none_if_missing() -> None:
    target_id = uuid4()
    with patch("app.predictions.ensemble_engine.MODELS_DIR", Path(tempfile.mkdtemp())):
        assert load_ensemble_model(target_id) is None


def test_load_ensemble_model_round_trips() -> None:
    feature_names = ["price_return_24h"]
    pipeline = _make_fitted_pipeline(feature_names)
    target_id = uuid4()
    bundle = {
        "pipeline": pipeline,
        "feature_names": feature_names,
        "n_samples": 60,
        "n_features": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "target_id": str(target_id),
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        model_file = tmp_path / f"{target_id}.pkl"
        with open(model_file, "wb") as f:
            pickle.dump(bundle, f)
        with patch("app.predictions.ensemble_engine.MODELS_DIR", tmp_path):
            loaded = load_ensemble_model(target_id)
    assert loaded is not None
    assert loaded["feature_names"] == feature_names
    assert loaded["n_samples"] == 60


# ---------------------------------------------------------------------------
# maybe_blend_with_ensemble
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_blend_returns_unchanged_when_no_model() -> None:
    snapshot = _make_snapshot({"price_return_24h": 0.01})
    pred_input = _make_prediction_input(snapshot, probability=0.7)
    with patch("app.predictions.ensemble_engine.MODELS_DIR", Path(tempfile.mkdtemp())):
        result = await maybe_blend_with_ensemble(pred_input)
    assert result is pred_input


@pytest.mark.asyncio
async def test_maybe_blend_blends_and_updates_rationale() -> None:
    feature_names = ["price_return_24h"]
    pipeline = _make_fitted_pipeline(feature_names)
    target_id = uuid4()
    bundle = {
        "pipeline": pipeline,
        "feature_names": feature_names,
        "n_samples": 60,
        "n_features": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "target_id": str(target_id),
    }

    snapshot = _make_snapshot({"price_return_24h": 0.01})
    pred_input = _make_prediction_input(snapshot, probability=0.7)
    # Override target id to match the bundle
    object.__setattr__(pred_input.target, "id", target_id)

    blend_mv_id = uuid4()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        model_file = tmp_path / f"{target_id}.pkl"
        with open(model_file, "wb") as f:
            pickle.dump(bundle, f)

        with (
            patch("app.predictions.ensemble_engine.MODELS_DIR", tmp_path),
            patch(
                "app.predictions.ensemble_engine.get_or_create_ensemble_blend_model_version",
                return_value=blend_mv_id,
            ),
        ):
            result = await maybe_blend_with_ensemble(pred_input)

    assert result is not pred_input
    assert "ensemble_probability" in result.rationale
    assert result.rationale["llm_probability"] == 0.7
    assert result.model_version_id == blend_mv_id
    assert 0.05 <= result.probability <= 0.95
    # blended should differ from pure LLM
    ensemble_prob = result.rationale["ensemble_probability"]
    expected_blended = blend_probabilities(0.7, ensemble_prob)
    assert abs(result.probability - expected_blended) < 1e-9
