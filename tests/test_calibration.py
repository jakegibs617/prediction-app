"""Unit tests for the isotonic regression calibration layer."""
from __future__ import annotations

import pickle
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import pytest
from sklearn.isotonic import IsotonicRegression

from app.predictions.calibration import (
    MIN_CALIBRATION_SAMPLES,
    apply_calibration,
    load_calibrator,
    maybe_apply_calibration,
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


def _make_fitted_calibrator(n: int = 60) -> IsotonicRegression:
    rng = np.random.default_rng(0)
    probs = np.sort(rng.uniform(0.1, 0.9, n))
    outcomes = (probs + rng.normal(0, 0.1, n) > 0.5).astype(float)
    cal = IsotonicRegression(out_of_bounds="clip")
    cal.fit(probs, outcomes)
    return cal


def _make_bundle(target_id, n: int = 60) -> dict:
    calibrator = _make_fitted_calibrator(n)
    return {
        "calibrator": calibrator,
        "n_samples": n,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "target_id": str(target_id),
        "prob_range": (0.1, 0.9),
        "mean_outcome": 0.5,
    }


def _make_snapshot() -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_id=uuid4(),
        asset_id=uuid4(),
        asset_symbol="BTC/USD",
        as_of_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        feature_set_name="price-baseline-v1",
        values=[
            FeatureValue(
                feature_key="price_return_24h",
                feature_type="numeric",
                numeric_value=0.02,
                available_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ],
    )


def _make_prediction_input(snapshot: FeatureSnapshot, probability: float = 0.75) -> PredictionInput:
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
        evidence_summary="Price returned 2% in 24h. Volume confirmed the move.",
        predicted_outcome="up_2pct",
        prediction_mode="live",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        correlation_id=uuid4(),
        rationale={"feature_count": 1, "model_name": "llm-prediction-engine"},
    )


# ---------------------------------------------------------------------------
# apply_calibration
# ---------------------------------------------------------------------------


def test_apply_calibration_returns_float_in_range() -> None:
    cal = _make_fitted_calibrator()
    result = apply_calibration(cal, 0.7)
    assert 0.05 <= result <= 0.95


def test_apply_calibration_clamps_below_minimum() -> None:
    cal = IsotonicRegression(out_of_bounds="clip")
    cal.fit([0.1, 0.9], [0.0, 0.0])
    result = apply_calibration(cal, 0.5)
    assert result == 0.05


def test_apply_calibration_clamps_above_maximum() -> None:
    cal = IsotonicRegression(out_of_bounds="clip")
    cal.fit([0.1, 0.9], [1.0, 1.0])
    result = apply_calibration(cal, 0.5)
    assert result == 0.95


def test_apply_calibration_preserves_monotonicity() -> None:
    cal = _make_fitted_calibrator()
    p_low = apply_calibration(cal, 0.3)
    p_high = apply_calibration(cal, 0.7)
    assert p_low <= p_high


# ---------------------------------------------------------------------------
# load_calibrator
# ---------------------------------------------------------------------------


def test_load_calibrator_returns_none_if_missing() -> None:
    with patch("app.predictions.calibration.CALIBRATORS_DIR", Path(tempfile.mkdtemp())):
        assert load_calibrator(uuid4()) is None


def test_load_calibrator_round_trips() -> None:
    target_id = uuid4()
    bundle = _make_bundle(target_id)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        model_file = tmp_path / f"{target_id}.pkl"
        with open(model_file, "wb") as f:
            pickle.dump(bundle, f)
        with patch("app.predictions.calibration.CALIBRATORS_DIR", tmp_path):
            loaded = load_calibrator(target_id)
    assert loaded is not None
    assert loaded["n_samples"] == bundle["n_samples"]
    assert isinstance(loaded["calibrator"], IsotonicRegression)


# ---------------------------------------------------------------------------
# maybe_apply_calibration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_apply_calibration_passthrough_when_no_calibrator() -> None:
    snapshot = _make_snapshot()
    pred_input = _make_prediction_input(snapshot, probability=0.75)
    with patch("app.predictions.calibration.CALIBRATORS_DIR", Path(tempfile.mkdtemp())):
        result = await maybe_apply_calibration(pred_input)
    assert result is pred_input


@pytest.mark.asyncio
async def test_maybe_apply_calibration_updates_probability_and_rationale() -> None:
    target_id = uuid4()
    bundle = _make_bundle(target_id)
    snapshot = _make_snapshot()
    pred_input = _make_prediction_input(snapshot, probability=0.75)
    object.__setattr__(pred_input.target, "id", target_id)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with open(tmp_path / f"{target_id}.pkl", "wb") as f:
            pickle.dump(bundle, f)
        with patch("app.predictions.calibration.CALIBRATORS_DIR", tmp_path):
            result = await maybe_apply_calibration(pred_input)

    assert result is not pred_input
    assert result.rationale["calibration_applied"] is True
    assert result.rationale["pre_calibration_probability"] == 0.75
    assert result.rationale["calibration_n_training_samples"] == bundle["n_samples"]
    assert 0.05 <= result.probability <= 0.95


@pytest.mark.asyncio
async def test_maybe_apply_calibration_preserves_other_rationale_fields() -> None:
    target_id = uuid4()
    bundle = _make_bundle(target_id)
    snapshot = _make_snapshot()
    pred_input = _make_prediction_input(snapshot, probability=0.6)
    object.__setattr__(pred_input.target, "id", target_id)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with open(tmp_path / f"{target_id}.pkl", "wb") as f:
            pickle.dump(bundle, f)
        with patch("app.predictions.calibration.CALIBRATORS_DIR", tmp_path):
            result = await maybe_apply_calibration(pred_input)

    assert result.rationale["feature_count"] == 1
    assert result.rationale["model_name"] == "llm-prediction-engine"
