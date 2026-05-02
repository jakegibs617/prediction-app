"""
Isotonic regression probability calibration layer.

Corrects systematic overconfidence in LLM (and blended) output probabilities by
fitting a monotone mapping from predicted probability → empirical frequency using
historical evaluation results.

Training: `python -m app.cli run calibration_train`
Inference: called automatically from predictions.service when a trained calibrator exists.
"""
from __future__ import annotations

import pickle
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import numpy as np
import structlog
from sklearn.isotonic import IsotonicRegression

from app.db.pool import get_pool
from app.predictions.contracts import PredictionInput

log = structlog.get_logger(__name__)

MIN_CALIBRATION_SAMPLES = 30
CALIBRATORS_DIR = Path("models/calibration")


# ---------------------------------------------------------------------------
# Filesystem model storage
# ---------------------------------------------------------------------------


def _calibrator_path(target_id: UUID) -> Path:
    return CALIBRATORS_DIR / f"{target_id}.pkl"


def load_calibrator(target_id: UUID) -> dict | None:
    """Load trained calibrator bundle from disk. Returns None if not trained yet."""
    path = _calibrator_path(target_id)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as exc:
        log.warning("calibrator_load_failed", target_id=str(target_id), error=str(exc))
        return None


def _save_calibrator(target_id: UUID, bundle: dict) -> None:
    CALIBRATORS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_calibrator_path(target_id), "wb") as f:
        pickle.dump(bundle, f)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def apply_calibration(calibrator: IsotonicRegression, probability: float) -> float:
    """Map a raw probability through the isotonic calibrator, clamped to [0.05, 0.95]."""
    calibrated = float(calibrator.predict([probability])[0])
    return max(0.05, min(0.95, round(calibrated, 5)))


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


async def _load_calibration_data(
    target_id: UUID,
    conn,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Load (probability, outcome) pairs for isotonic regression.

    Returns (probs, outcomes) arrays sorted by probability (required by IsotonicRegression),
    or None if insufficient data.
    """
    rows = await conn.fetch(
        """
        SELECT COALESCE(pre_cal_probability, final_probability) AS training_probability,
               directional_correct
        FROM ml.training_examples
        WHERE target_id = $1
        ORDER BY COALESCE(pre_cal_probability, final_probability) ASC
        """,
        target_id,
    )
    if len(rows) < MIN_CALIBRATION_SAMPLES:
        return None

    probs = np.array([float(row["training_probability"]) for row in rows], dtype=float)
    outcomes = np.array([1 if row["directional_correct"] else 0 for row in rows], dtype=float)
    return probs, outcomes


async def train_target_calibrator(target_id: UUID, target_name: str) -> bool:
    """Fit an IsotonicRegression calibrator for one target. Returns True on success."""
    pool = get_pool()
    async with pool.acquire() as conn:
        data = await _load_calibration_data(target_id, conn)

    if data is None:
        log.info(
            "calibration_train_skipped_insufficient_data",
            target=target_name,
            min_samples=MIN_CALIBRATION_SAMPLES,
        )
        return False

    probs, outcomes = data

    if len(set(outcomes.tolist())) < 2:
        log.info("calibration_train_skipped_single_class", target=target_name, n_samples=len(outcomes))
        return False

    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(probs, outcomes)

    bundle = {
        "calibrator": calibrator,
        "n_samples": int(len(outcomes)),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "target_id": str(target_id),
        "prob_range": (float(probs.min()), float(probs.max())),
        "mean_outcome": float(outcomes.mean()),
    }
    _save_calibrator(target_id, bundle)

    log.info(
        "calibration_train_complete",
        target=target_name,
        n_samples=len(outcomes),
        mean_outcome=bundle["mean_outcome"],
        prob_range=bundle["prob_range"],
    )
    return True


async def train_all_calibrators() -> None:
    """Train calibrators for all active targets that have enough evaluation history."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM predictions.prediction_targets WHERE is_active = true ORDER BY created_at ASC"
        )

    trained = 0
    skipped = 0
    for row in rows:
        success = await train_target_calibrator(row["id"], row["name"])
        if success:
            trained += 1
        else:
            skipped += 1

    log.info("calibration_train_all_complete", trained=trained, skipped=skipped)


# ---------------------------------------------------------------------------
# Calibration entry point (called from predictions.service)
# ---------------------------------------------------------------------------


async def maybe_apply_calibration(prediction_input: PredictionInput) -> PredictionInput:
    """Apply isotonic calibration to the prediction probability if a trained calibrator exists.

    Falls through unchanged when no calibrator is available.
    """
    target_id = prediction_input.target.id
    bundle = load_calibrator(target_id)
    if bundle is None:
        return prediction_input

    raw_prob = prediction_input.probability
    calibrated_prob = apply_calibration(bundle["calibrator"], raw_prob)

    updated_rationale = {
        **prediction_input.rationale,
        "calibration_applied": True,
        "pre_calibration_probability": round(raw_prob, 5),
        "calibration_n_training_samples": bundle.get("n_samples"),
        "calibration_trained_at": bundle.get("trained_at"),
    }

    log.info(
        "calibration_applied",
        asset=prediction_input.asset_symbol,
        target=prediction_input.target.name,
        raw_probability=raw_prob,
        calibrated_probability=calibrated_prob,
    )

    return prediction_input.model_copy(update={
        "probability": calibrated_prob,
        "rationale": updated_rationale,
    })
