from __future__ import annotations

from datetime import datetime, timedelta


def is_evaluable(horizon_end_at: datetime, evaluated_at: datetime) -> bool:
    return evaluated_at >= horizon_end_at


def get_next_trading_day_close(issuance_time: datetime) -> datetime:
    current = issuance_time
    while current.weekday() >= 5:
        current += timedelta(days=1)

    next_day = current + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)

    return next_day.replace(hour=16, minute=0, second=0, microsecond=0)


def compute_directional_accuracy(predicted_direction: str, actual_return: float) -> bool:
    if predicted_direction == "up":
        return actual_return > 0
    if predicted_direction == "down":
        return actual_return < 0
    if predicted_direction == "neutral":
        return actual_return == 0
    raise ValueError(f"unsupported predicted_direction: {predicted_direction}")


def compute_brier_score(probability: float, outcome: bool | int) -> float:
    if probability < 0 or probability > 1:
        raise ValueError("probability must be between 0 and 1")
    outcome_value = int(outcome)
    return (probability - outcome_value) ** 2


def compute_calibration_bucket(probability: float) -> str:
    if probability < 0 or probability > 1:
        raise ValueError("probability must be between 0 and 1")

    if probability == 1:
        return "0.90-1.00"

    start = int(probability * 10) / 10
    end = start + 0.1
    return f"{start:.2f}-{end:.2f}"


def compute_paper_return(predicted_direction: str, actual_return: float, cost_bps: float) -> float:
    gross_return = actual_return if predicted_direction == "up" else -actual_return
    if predicted_direction == "neutral":
        gross_return = 0.0
    return gross_return - (cost_bps / 10_000)
