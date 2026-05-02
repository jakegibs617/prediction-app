"""
Statistical ensemble model: scikit-learn LogisticRegression trained on historical
feature_values × evaluation_results. Blended with LLM probability at prediction time.

Training: `python -m app.cli run ensemble_train`
Inference: called automatically from predictions.service when a trained model exists.
"""
from __future__ import annotations

import json
import pickle
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import numpy as np
import structlog
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.db.pool import get_pool
from app.predictions.contracts import FeatureSnapshot, PredictionInput

log = structlog.get_logger(__name__)

ENSEMBLE_BLEND_MODEL_NAME = "ensemble-blend"
ENSEMBLE_BLEND_MODEL_VERSION = "v1.0"
MIN_TRAINING_SAMPLES = 50
LLM_WEIGHT = 0.6
ENSEMBLE_WEIGHT = 0.4

MODELS_DIR = Path("models/ensemble")


# ---------------------------------------------------------------------------
# Filesystem model storage
# ---------------------------------------------------------------------------


def _model_path(target_id: UUID) -> Path:
    return MODELS_DIR / f"{target_id}.pkl"


def load_ensemble_model(target_id: UUID) -> dict | None:
    """Load trained model bundle from disk. Returns None if not trained yet."""
    path = _model_path(target_id)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as exc:
        log.warning("ensemble_model_load_failed", target_id=str(target_id), error=str(exc))
        return None


def _save_ensemble_model(target_id: UUID, bundle: dict) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_model_path(target_id), "wb") as f:
        pickle.dump(bundle, f)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def _predict_probability(
    pipeline: Pipeline,
    feature_names: list[str],
    snapshot: FeatureSnapshot,
) -> float | None:
    """Compute ensemble probability from a feature snapshot. Returns None on failure."""
    snap_feats = {
        v.feature_key: v.numeric_value
        for v in snapshot.values
        if v.numeric_value is not None
    }
    x = np.array(
        [snap_feats.get(f, 0.0) for f in feature_names],
        dtype=float,
    ).reshape(1, -1)
    try:
        prob = float(pipeline.predict_proba(x)[0, 1])
    except Exception as exc:
        log.warning("ensemble_predict_failed", error=str(exc))
        return None
    return max(0.05, min(0.95, prob))


def blend_probabilities(llm_prob: float, ensemble_prob: float) -> float:
    """Weighted average of LLM and ensemble probabilities, clamped to [0.05, 0.95]."""
    blended = LLM_WEIGHT * llm_prob + ENSEMBLE_WEIGHT * ensemble_prob
    return max(0.05, min(0.95, round(blended, 5)))


# ---------------------------------------------------------------------------
# DB: model version registry
# ---------------------------------------------------------------------------


async def get_or_create_ensemble_blend_model_version() -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM predictions.model_versions WHERE name = $1 AND version = $2",
            ENSEMBLE_BLEND_MODEL_NAME,
            ENSEMBLE_BLEND_MODEL_VERSION,
        )
        if existing:
            return existing
        config = json.dumps({
            "engine": "ensemble-blend",
            "component_models": [
                {"name": "llm-prediction-engine", "weight": LLM_WEIGHT},
                {"name": "statistical-logreg", "weight": ENSEMBLE_WEIGHT},
            ],
            "blend_strategy": "weighted_average",
            "min_training_samples": MIN_TRAINING_SAMPLES,
        })
        return await conn.fetchval(
            """
            INSERT INTO predictions.model_versions (name, version, model_type, config)
            VALUES ($1, $2, 'ensemble', $3)
            RETURNING id
            """,
            ENSEMBLE_BLEND_MODEL_NAME,
            ENSEMBLE_BLEND_MODEL_VERSION,
            config,
        )


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


async def _load_training_data(
    target_id: UUID,
    conn,
) -> tuple[np.ndarray, np.ndarray, list[str]] | None:
    """Build feature matrix and label vector from evaluation history.

    Returns (X, y, feature_names) or None if insufficient data.
    """
    pred_rows = await conn.fetch(
        """
        SELECT feature_snapshot_id, directional_correct
        FROM ml.training_examples
        WHERE target_id = $1
        ORDER BY created_at ASC
        """,
        target_id,
    )
    if len(pred_rows) < MIN_TRAINING_SAMPLES:
        return None

    snapshot_ids = [row["feature_snapshot_id"] for row in pred_rows]
    fv_rows = await conn.fetch(
        """
        SELECT snapshot_id, feature_key, numeric_value
        FROM features.feature_values
        WHERE snapshot_id = ANY($1)
          AND feature_type = 'numeric'
          AND numeric_value IS NOT NULL
        """,
        snapshot_ids,
    )

    fv_by_snapshot: dict[UUID, dict[str, float]] = defaultdict(dict)
    for row in fv_rows:
        fv_by_snapshot[row["snapshot_id"]][row["feature_key"]] = float(row["numeric_value"])

    all_feature_names = sorted({
        k for feats in fv_by_snapshot.values() for k in feats
    })
    if not all_feature_names:
        return None

    X = np.array(
        [
            [fv_by_snapshot.get(row["feature_snapshot_id"], {}).get(f, 0.0) for f in all_feature_names]
            for row in pred_rows
        ],
        dtype=float,
    )
    y = np.array([1 if row["directional_correct"] else 0 for row in pred_rows], dtype=int)
    return X, y, all_feature_names


async def train_target_ensemble(target_id: UUID, target_name: str) -> bool:
    """Train a LogisticRegression for one target. Returns True on success."""
    pool = get_pool()
    async with pool.acquire() as conn:
        data = await _load_training_data(target_id, conn)

    if data is None:
        log.info(
            "ensemble_train_skipped_insufficient_data",
            target=target_name,
            min_samples=MIN_TRAINING_SAMPLES,
        )
        return False

    X, y, feature_names = data

    if len(set(y.tolist())) < 2:
        log.info("ensemble_train_skipped_single_class", target=target_name, n_samples=len(y))
        return False

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, C=1.0, random_state=42)),
    ])
    pipeline.fit(X, y)

    bundle = {
        "pipeline": pipeline,
        "feature_names": feature_names,
        "n_samples": int(len(y)),
        "n_features": len(feature_names),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "target_id": str(target_id),
    }
    _save_ensemble_model(target_id, bundle)

    log.info(
        "ensemble_train_complete",
        target=target_name,
        n_samples=len(y),
        n_features=len(feature_names),
    )
    return True


async def train_all_targets() -> None:
    """Train ensemble models for all active targets that have enough evaluation history."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM predictions.prediction_targets WHERE is_active = true ORDER BY created_at ASC"
        )

    trained = 0
    skipped = 0
    for row in rows:
        success = await train_target_ensemble(row["id"], row["name"])
        if success:
            trained += 1
        else:
            skipped += 1

    log.info("ensemble_train_all_complete", trained=trained, skipped=skipped)


# ---------------------------------------------------------------------------
# Blend entry point (called from predictions.service)
# ---------------------------------------------------------------------------


async def maybe_blend_with_ensemble(prediction_input: PredictionInput) -> PredictionInput:
    """Return a blended PredictionInput if a trained ensemble model exists for this target.

    Falls through unchanged when no model is available, leaving the caller's
    pure-LLM prediction intact.
    """
    target_id = prediction_input.target.id
    bundle = load_ensemble_model(target_id)
    if bundle is None:
        return prediction_input

    ensemble_prob = _predict_probability(
        bundle["pipeline"],
        bundle["feature_names"],
        prediction_input.feature_snapshot,
    )
    if ensemble_prob is None:
        return prediction_input

    llm_prob = prediction_input.probability
    blended = blend_probabilities(llm_prob, ensemble_prob)
    blend_model_version_id = await get_or_create_ensemble_blend_model_version()

    updated_rationale = {
        **prediction_input.rationale,
        "llm_probability": round(llm_prob, 5),
        "ensemble_probability": round(ensemble_prob, 5),
        "blend_weights": {"llm": LLM_WEIGHT, "ensemble": ENSEMBLE_WEIGHT},
        "ensemble_n_training_samples": bundle.get("n_samples"),
        "ensemble_trained_at": bundle.get("trained_at"),
    }

    log.info(
        "ensemble_blend_applied",
        asset=prediction_input.asset_symbol,
        target=prediction_input.target.name,
        llm_probability=llm_prob,
        ensemble_probability=ensemble_prob,
        blended_probability=blended,
    )

    return prediction_input.model_copy(update={
        "probability": blended,
        "model_version_id": blend_model_version_id,
        "rationale": updated_rationale,
    })
