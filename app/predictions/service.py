from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog

from app.db.pool import get_pool
from app.model_client.factory import get_model_client
from app.predictions.contracts import DirectionRule, FeatureSnapshot, FeatureValue, PredictionTarget, SettlementRule
from app.predictions.heuristic import HEURISTIC_MODEL_NAME, HEURISTIC_MODEL_VERSION, generate_heuristic_prediction_input
from app.predictions.llm_engine import (
    LLM_MODEL_NAME,
    LLM_MODEL_VERSION,
    generate_llm_prediction_input,
    get_or_create_llm_model_version,
)
from app.predictions.logic import PredictionRecord, build_prediction_record
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PredictionCandidate:
    target: PredictionTarget
    snapshot: FeatureSnapshot
    asset_type: str


def _parse_target_rule(rule_value: str | dict, schema):
    if isinstance(rule_value, dict):
        return schema.model_validate(rule_value)
    return schema.model_validate(json.loads(rule_value))


async def read_active_targets() -> list[PredictionTarget]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, asset_type, target_metric, horizon_hours, direction_rule, settlement_rule, asset_id, is_active
            FROM predictions.prediction_targets
            WHERE is_active = true
            ORDER BY created_at ASC
            """
        )
    return [
        PredictionTarget(
            id=row["id"],
            name=row["name"],
            asset_type=row["asset_type"],
            target_metric=row["target_metric"],
            horizon_hours=row["horizon_hours"],
            direction_rule=_parse_target_rule(row["direction_rule"], DirectionRule),
            settlement_rule=_parse_target_rule(row["settlement_rule"], SettlementRule),
            asset_id=row["asset_id"],
            is_active=row["is_active"],
        )
        for row in rows
    ]


async def get_or_create_model_version() -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT id
            FROM predictions.model_versions
            WHERE name = $1 AND version = $2
            """,
            HEURISTIC_MODEL_NAME,
            HEURISTIC_MODEL_VERSION,
        )
        if existing:
            return existing

        return await conn.fetchval(
            """
            INSERT INTO predictions.model_versions (name, version, model_type, config)
            VALUES ($1, $2, 'heuristic', $3)
            RETURNING id
            """,
            HEURISTIC_MODEL_NAME,
            HEURISTIC_MODEL_VERSION,
            json.dumps({"engine": "heuristic", "strategy": "mean_reversion"}),
        )


async def read_latest_snapshot_for_asset(asset_id: UUID) -> FeatureSnapshot | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        snapshot_row = await conn.fetchrow(
            """
            SELECT
                fs.id,
                fs.asset_id,
                a.symbol AS asset_symbol,
                fs.as_of_at,
                fset.name,
                fset.version
            FROM features.feature_snapshots fs
            JOIN features.feature_sets fset ON fset.id = fs.feature_set_id
            JOIN market_data.assets a ON a.id = fs.asset_id
            WHERE fs.asset_id = $1
            ORDER BY fs.as_of_at DESC
            LIMIT 1
            """,
            asset_id,
        )
        if snapshot_row is None:
            return None

        value_rows = await conn.fetch(
            """
            SELECT feature_key, feature_type, numeric_value, text_value, boolean_value, json_value, available_at
            FROM features.feature_values
            WHERE snapshot_id = $1
            ORDER BY feature_key ASC
            """,
            snapshot_row["id"],
        )
        lineage_rows = await conn.fetch(
            """
            SELECT source_record_id
            FROM features.feature_lineage
            WHERE snapshot_id = $1
            """,
            snapshot_row["id"],
        )

    source_record_ids = [
        row["source_record_id"]
        for row in lineage_rows
        if row["source_record_id"] is not None
    ]

    values = [
        FeatureValue(
            feature_key=row["feature_key"],
            feature_type=row["feature_type"],
            numeric_value=float(row["numeric_value"]) if row["numeric_value"] is not None else None,
            text_value=row["text_value"],
            boolean_value=row["boolean_value"],
            json_value=row["json_value"],
            available_at=row["available_at"],
            source_record_ids=source_record_ids,
        )
        for row in value_rows
    ]
    return FeatureSnapshot(
        snapshot_id=snapshot_row["id"],
        asset_id=snapshot_row["asset_id"],
        asset_symbol=snapshot_row["asset_symbol"],
        as_of_at=snapshot_row["as_of_at"],
        feature_set_name=f"{snapshot_row['name']}-{snapshot_row['version']}",
        values=values,
    )


async def read_prediction_candidates() -> list[PredictionCandidate]:
    pool = get_pool()
    async with pool.acquire() as conn:
        asset_rows = await conn.fetch(
            """
            SELECT id, symbol, asset_type
            FROM market_data.assets
            WHERE is_active = true
            ORDER BY created_at ASC
            """
        )
    targets = await read_active_targets()
    candidates: list[PredictionCandidate] = []
    for target in targets:
        for asset in asset_rows:
            if asset["asset_type"] != target.asset_type:
                continue
            if target.asset_id is not None and target.asset_id != asset["id"]:
                continue
            snapshot = await read_latest_snapshot_for_asset(asset["id"])
            if snapshot is None:
                continue
            candidates.append(
                PredictionCandidate(
                    target=target,
                    snapshot=snapshot,
                    asset_type=asset["asset_type"],
                )
            )
    return candidates


async def prediction_exists(*, target_id: UUID, feature_snapshot_id: UUID, prediction_mode: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT 1
            FROM predictions.predictions
            WHERE target_id = $1
              AND feature_snapshot_id = $2
              AND prediction_mode = $3
            """,
            target_id,
            feature_snapshot_id,
            prediction_mode,
        )
    return bool(existing)


async def write_prediction_record(prediction: PredictionRecord) -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO predictions.predictions
                (id, target_id, asset_id, feature_snapshot_id, model_version_id, prompt_version_id, prediction_mode,
                 predicted_outcome, probability, evidence_summary, rationale, created_at, horizon_end_at,
                 correlation_id, hallucination_risk, probability_extreme_flag, context_compressed, backtest_run_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
            RETURNING id
            """,
            prediction.id,
            prediction.target_id,
            prediction.asset_id,
            prediction.feature_snapshot_id,
            prediction.model_version_id,
            prediction.prompt_version_id,
            prediction.prediction_mode,
            prediction.predicted_outcome,
            prediction.probability,
            prediction.evidence_summary,
            json.dumps(prediction.rationale),
            prediction.created_at,
            prediction.horizon_end_at,
            prediction.correlation_id,
            prediction.hallucination_risk,
            prediction.probability_extreme_flag,
            prediction.context_compressed,
            prediction.backtest_run_id,
        )


async def write_prediction_status(prediction_id: UUID, status: str, reason: str | None = None) -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO predictions.prediction_status_history (prediction_id, status, reason, recorded_at)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            prediction_id,
            status,
            reason,
            get_utc_now(),
        )


async def generate_prediction_for_candidate(
    candidate: PredictionCandidate,
    *,
    correlation_id: UUID,
    prediction_mode: str = "live",
    created_at: datetime | None = None,
) -> PredictionRecord | None:
    if await prediction_exists(
        target_id=candidate.target.id,
        feature_snapshot_id=candidate.snapshot.snapshot_id,
        prediction_mode=prediction_mode,
    ):
        return None

    issued_at = created_at or get_utc_now()

    # Try LLM engine first; fall back to heuristic on any failure
    try:
        model_client = get_model_client()
        model_version_id = await get_or_create_llm_model_version(LLM_MODEL_NAME, LLM_MODEL_VERSION)
        prediction_input = await generate_llm_prediction_input(
            target=candidate.target,
            snapshot=candidate.snapshot,
            asset_type=candidate.asset_type,
            model_client=model_client,
            model_version_id=model_version_id,
            created_at=issued_at,
            correlation_id=correlation_id,
            prediction_mode=prediction_mode,
        )
        status_reason = "llm prediction issued"
    except Exception as exc:
        log.warning("llm_prediction_failed_falling_back", error=str(exc), exc_info=True)
        model_version_id = await get_or_create_model_version()
        prediction_input = generate_heuristic_prediction_input(
            target=candidate.target,
            snapshot=candidate.snapshot,
            asset_type=candidate.asset_type,
            model_version_id=model_version_id,
            created_at=issued_at,
            correlation_id=correlation_id,
            prediction_mode=prediction_mode,
        )
        status_reason = "heuristic fallback prediction issued"

    prediction_record = build_prediction_record(prediction_input)
    await write_prediction_record(prediction_record)
    await write_prediction_status(prediction_record.id, "created", status_reason)
    return prediction_record
