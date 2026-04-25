"""
Accuracy report — computes rolling prediction accuracy metrics over evaluated
predictions and sends a summary to Telegram.

Triggered by EvaluationPipeline after any run that settles new predictions.
Only live (non-backtest) predictions that are at least 3 days old are included,
ensuring the market has had sufficient time to resolve before we score them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import structlog

from app.alerts.telegram import send_telegram_message
from app.config import settings
from app.db.pool import get_pool
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

_MIN_AGE_DAYS = 3
_ROLLING_WINDOW_DAYS = 30


@dataclass(frozen=True)
class AccuracySummary:
    total_evaluated: int
    directional_correct: int
    directional_accuracy_pct: float
    mean_brier_score: float
    window_days: int


@dataclass(frozen=True)
class PerTargetRow:
    target_name: str
    count: int
    directional_accuracy_pct: float
    mean_brier_score: float


async def compute_accuracy_summary(window_days: int = _ROLLING_WINDOW_DAYS) -> AccuracySummary | None:
    pool = get_pool()
    cutoff = get_utc_now() - timedelta(days=window_days)
    min_age_cutoff = get_utc_now() - timedelta(days=_MIN_AGE_DAYS)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                                AS total_evaluated,
                SUM(CASE WHEN er.directional_correct THEN 1 ELSE 0 END) AS directional_correct,
                AVG(er.brier_score::numeric)                            AS mean_brier_score
            FROM evaluation.evaluation_results er
            JOIN predictions.predictions p ON p.id = er.prediction_id
            WHERE er.evaluation_state = 'evaluated'
              AND er.directional_correct IS NOT NULL
              AND p.backtest_run_id IS NULL
              AND p.created_at >= $1
              AND p.created_at <= $2
            """,
            cutoff,
            min_age_cutoff,
        )

    if not row or not row["total_evaluated"]:
        return None

    total = int(row["total_evaluated"])
    correct = int(row["directional_correct"])
    return AccuracySummary(
        total_evaluated=total,
        directional_correct=correct,
        directional_accuracy_pct=correct / total * 100,
        mean_brier_score=float(row["mean_brier_score"]),
        window_days=window_days,
    )


async def compute_per_target_rows(window_days: int = _ROLLING_WINDOW_DAYS) -> list[PerTargetRow]:
    pool = get_pool()
    cutoff = get_utc_now() - timedelta(days=window_days)
    min_age_cutoff = get_utc_now() - timedelta(days=_MIN_AGE_DAYS)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                t.name                                                       AS target_name,
                COUNT(*)                                                     AS count,
                AVG(CASE WHEN er.directional_correct THEN 1.0 ELSE 0.0 END) AS dir_acc,
                AVG(er.brier_score::numeric)                                 AS mean_brier
            FROM evaluation.evaluation_results er
            JOIN predictions.predictions p ON p.id = er.prediction_id
            JOIN predictions.prediction_targets t ON t.id = p.target_id
            WHERE er.evaluation_state = 'evaluated'
              AND er.directional_correct IS NOT NULL
              AND p.backtest_run_id IS NULL
              AND p.created_at >= $1
              AND p.created_at <= $2
            GROUP BY t.name
            ORDER BY dir_acc DESC
            """,
            cutoff,
            min_age_cutoff,
        )

    return [
        PerTargetRow(
            target_name=row["target_name"],
            count=int(row["count"]),
            directional_accuracy_pct=float(row["dir_acc"]) * 100,
            mean_brier_score=float(row["mean_brier"]),
        )
        for row in rows
    ]


def format_accuracy_report(summary: AccuracySummary, per_target: list[PerTargetRow]) -> str:
    lines = [
        "<b>Prediction Accuracy Report</b>",
        f"Rolling {summary.window_days}-day window (live predictions ≥ {_MIN_AGE_DAYS} days old)",
        "",
        f"Evaluated: <b>{summary.total_evaluated}</b>",
        f"Directional accuracy: <b>{summary.directional_accuracy_pct:.1f}%</b> "
        f"({summary.directional_correct}/{summary.total_evaluated} correct)",
        f"Mean Brier score: <b>{summary.mean_brier_score:.4f}</b> "
        "(lower is better; random = 0.25)",
    ]

    if per_target:
        lines.append("")
        lines.append("<b>By target:</b>")
        for row in per_target:
            lines.append(
                f"  • {row.target_name}: {row.directional_accuracy_pct:.0f}% correct "
                f"({row.count} predictions, Brier {row.mean_brier_score:.4f})"
            )

    return "\n".join(lines)


async def send_accuracy_report() -> bool:
    summary = await compute_accuracy_summary()
    if summary is None:
        log.info("accuracy_report_skipped_no_data")
        return False

    per_target = await compute_per_target_rows()
    message = format_accuracy_report(summary, per_target)

    result = await send_telegram_message(settings.telegram_chat_id, message)
    if result.success:
        log.info(
            "accuracy_report_sent",
            total_evaluated=summary.total_evaluated,
            directional_accuracy_pct=round(summary.directional_accuracy_pct, 1),
            mean_brier_score=round(summary.mean_brier_score, 4),
        )
    else:
        log.error("accuracy_report_send_failed", error=result.error)

    return result.success
