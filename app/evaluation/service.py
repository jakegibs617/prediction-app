from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.db.pool import get_pool
from app.evaluation.scoring import (
    compute_brier_score,
    compute_calibration_bucket,
    compute_directional_accuracy,
    compute_paper_return,
    get_next_trading_day_close,
    is_evaluable,
)
from app.predictions.contracts import DirectionRule, SettlementRule
from app.utils.time import get_utc_now


@dataclass(frozen=True)
class EvaluationCandidate:
    prediction_id: UUID
    asset_id: UUID
    created_at: datetime
    horizon_end_at: datetime
    probability: Decimal
    predicted_outcome: str
    target_metric: str
    asset_type: str
    direction_rule: DirectionRule
    settlement_rule: SettlementRule


def _parse_rule(rule_value: str | dict, schema):
    if isinstance(rule_value, dict):
        return schema.model_validate(rule_value)
    return schema.model_validate(json.loads(rule_value))


def build_settlement_time(candidate: EvaluationCandidate) -> datetime:
    if candidate.settlement_rule.type == "trading_day_close":
        horizon = candidate.horizon_end_at
        same_day_close = horizon.replace(hour=16, minute=0, second=0, microsecond=0)
        if horizon.weekday() < 5 and horizon <= same_day_close:
            return same_day_close

        current = horizon
        while current.weekday() >= 5:
            current = current.replace(hour=0, minute=0, second=0, microsecond=0)
            current += timedelta(days=1)

        if current.weekday() < 5 and current.date() != horizon.date():
            return current.replace(hour=16, minute=0, second=0, microsecond=0)

        return get_next_trading_day_close(horizon)
    return candidate.horizon_end_at


def compute_actual_outcome(candidate: EvaluationCandidate, actual_return: float) -> bool:
    threshold = candidate.direction_rule.threshold or 0.0
    direction = candidate.direction_rule.direction
    if direction == "up":
        return actual_return >= threshold
    if direction == "down":
        return actual_return <= -threshold
    return actual_return == 0.0


async def read_evaluation_candidates() -> list[EvaluationCandidate]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.id AS prediction_id,
                p.asset_id,
                p.created_at,
                p.horizon_end_at,
                p.probability,
                p.predicted_outcome,
                t.target_metric,
                t.asset_type,
                t.direction_rule,
                t.settlement_rule
            FROM predictions.predictions p
            JOIN predictions.prediction_targets t ON t.id = p.target_id
            LEFT JOIN evaluation.evaluation_results er ON er.prediction_id = p.id
            WHERE er.prediction_id IS NULL
               OR er.evaluation_state = 'not_evaluable'
            ORDER BY p.horizon_end_at ASC
            """
        )
    return [
        EvaluationCandidate(
            prediction_id=row["prediction_id"],
            asset_id=row["asset_id"],
            created_at=row["created_at"],
            horizon_end_at=row["horizon_end_at"],
            probability=Decimal(row["probability"]),
            predicted_outcome=row["predicted_outcome"],
            target_metric=row["target_metric"],
            asset_type=row["asset_type"],
            direction_rule=_parse_rule(row["direction_rule"], DirectionRule),
            settlement_rule=_parse_rule(row["settlement_rule"], SettlementRule),
        )
        for row in rows
    ]


async def get_price_at_or_before(asset_id: UUID, at_time: datetime) -> float | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT close
            FROM market_data.price_bars
            WHERE asset_id = $1
              AND bar_end_at <= $2
            ORDER BY bar_end_at DESC
            LIMIT 1
            """,
            asset_id,
            at_time,
        )
    return float(row["close"]) if row else None


async def write_evaluation_result(
    *,
    prediction_id: UUID,
    evaluation_state: str,
    actual_outcome: str | None = None,
    directional_correct: bool | None = None,
    brier_score: float | None = None,
    return_pct: float | None = None,
    cost_adjusted_return_pct: float | None = None,
    calibration_bucket: str | None = None,
    notes: str | None = None,
) -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        result_id = await conn.fetchval(
            """
            INSERT INTO evaluation.evaluation_results
                (prediction_id, evaluated_at, evaluation_state, actual_outcome, directional_correct,
                 brier_score, return_pct, cost_adjusted_return_pct, calibration_bucket, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (prediction_id)
            DO UPDATE SET
                evaluated_at = EXCLUDED.evaluated_at,
                evaluation_state = EXCLUDED.evaluation_state,
                actual_outcome = EXCLUDED.actual_outcome,
                directional_correct = EXCLUDED.directional_correct,
                brier_score = EXCLUDED.brier_score,
                return_pct = EXCLUDED.return_pct,
                cost_adjusted_return_pct = EXCLUDED.cost_adjusted_return_pct,
                calibration_bucket = EXCLUDED.calibration_bucket,
                notes = EXCLUDED.notes
            RETURNING id
            """,
            prediction_id,
            get_utc_now(),
            evaluation_state,
            actual_outcome,
            directional_correct,
            brier_score,
            return_pct,
            cost_adjusted_return_pct,
            calibration_bucket,
            notes,
        )
    return result_id


async def evaluate_prediction(candidate: EvaluationCandidate, *, evaluated_at: datetime | None = None) -> str:
    now = evaluated_at or get_utc_now()
    settlement_time = build_settlement_time(candidate)
    if not is_evaluable(settlement_time, now):
        await write_evaluation_result(
            prediction_id=candidate.prediction_id,
            evaluation_state="not_evaluable",
            notes="prediction horizon has not reached settlement time yet",
        )
        return "not_evaluable"

    start_price = await get_price_at_or_before(candidate.asset_id, candidate.created_at)
    end_price = await get_price_at_or_before(candidate.asset_id, settlement_time)
    if start_price is None or end_price is None:
        await write_evaluation_result(
            prediction_id=candidate.prediction_id,
            evaluation_state="void",
            notes="missing price data for settlement",
        )
        return "void"

    actual_return = (end_price - start_price) / start_price
    outcome_hit = compute_actual_outcome(candidate, actual_return)
    directional_correct = compute_directional_accuracy(candidate.direction_rule.direction, actual_return)
    await write_evaluation_result(
        prediction_id=candidate.prediction_id,
        evaluation_state="evaluated",
        actual_outcome="hit" if outcome_hit else "miss",
        directional_correct=directional_correct,
        brier_score=compute_brier_score(float(candidate.probability), outcome_hit),
        return_pct=actual_return,
        cost_adjusted_return_pct=compute_paper_return(candidate.direction_rule.direction, actual_return, cost_bps=10),
        calibration_bucket=compute_calibration_bucket(float(candidate.probability)),
        notes=f"settled using prices at {candidate.created_at.isoformat()} and {settlement_time.isoformat()}",
    )
    return "evaluated"
