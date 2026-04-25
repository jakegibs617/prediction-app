from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.evaluation.scoring import (
    compute_brier_score,
    compute_calibration_bucket,
    compute_directional_accuracy,
    get_next_trading_day_close,
    is_evaluable,
)


def test_evaluates_only_after_horizon_end() -> None:
    horizon_end = datetime(2026, 4, 19, 14, 1, tzinfo=UTC)
    assert not is_evaluable(horizon_end, datetime(2026, 4, 19, 14, 0, 59, tzinfo=UTC))
    assert is_evaluable(horizon_end, horizon_end)


def test_settles_equity_target_across_weekend() -> None:
    friday_evening = datetime(2026, 4, 17, 20, 0, tzinfo=UTC)
    assert get_next_trading_day_close(friday_evening) == datetime(2026, 4, 20, 16, 0, tzinfo=UTC)


def test_compute_directional_accuracy_correctly() -> None:
    assert compute_directional_accuracy("up", 0.02)
    assert compute_directional_accuracy("down", -0.01)
    assert not compute_directional_accuracy("up", -0.01)


def test_compute_brier_score_correctly() -> None:
    assert compute_brier_score(0.8, True) == pytest.approx(0.04)


def test_compute_calibration_bucket_correctly() -> None:
    assert compute_calibration_bucket(0.87) == "0.80-0.90"
