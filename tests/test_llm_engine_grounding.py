"""Tests for _check_evidence_grounding and calibration helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.predictions.llm_engine import (
    _build_calibration_block,
    _check_evidence_grounding,
    _compute_macro_yield_curve_slope,
)
from app.predictions.contracts import FeatureSnapshot


def _empty_snapshot() -> FeatureSnapshot:
    snap = MagicMock(spec=FeatureSnapshot)
    snap.asset_symbol = "BTC/USD"
    snap.as_of_at = datetime(2026, 4, 25, tzinfo=timezone.utc)
    snap.values = []
    return snap


def test_grounded_macro_value_does_not_flag() -> None:
    macro_rows = [{"value": 31.0, "series_id": "FEAR_GREED"}]
    evidence = "The Fear & Greed index stands at 31.0, indicating fear."
    assert not _check_evidence_grounding(
        evidence, _empty_snapshot(), macro_rows=macro_rows
    )


def test_ungrounded_macro_value_flags_hallucination() -> None:
    macro_rows = [{"value": 31.0, "series_id": "FEAR_GREED"}]
    # 78.5 does not appear in macro_rows or snapshot
    evidence = "The Fear & Greed index is at 78.5, showing extreme greed."
    assert _check_evidence_grounding(
        evidence, _empty_snapshot(), macro_rows=macro_rows
    )


def test_macro_value_in_percent_form_does_not_flag() -> None:
    # FEDFUNDS as a fraction (0.0533) — LLM may render as 5.33%
    macro_rows = [{"value": 0.0533, "series_id": "FEDFUNDS"}]
    evidence = "The fed funds rate is 5.33%, a restrictive stance."
    assert not _check_evidence_grounding(
        evidence, _empty_snapshot(), macro_rows=macro_rows
    )


def test_none_macro_value_is_skipped_gracefully() -> None:
    macro_rows = [{"value": None, "series_id": "FEDFUNDS"}]
    evidence = "The fed funds rate is 5.33%."
    # 5.33 is not grounded — should flag, not crash
    assert _check_evidence_grounding(
        evidence, _empty_snapshot(), macro_rows=macro_rows
    )


def test_empty_macro_rows_does_not_affect_feature_grounding() -> None:
    snap = _empty_snapshot()
    feat = MagicMock()
    feat.feature_key = "rsi_14"
    feat.numeric_value = 62.5
    feat.text_value = None
    snap.values = [feat]

    evidence = "RSI is at 62.5, approaching overbought territory."
    assert not _check_evidence_grounding(evidence, snap, macro_rows=[])


# --- _build_calibration_block ---

def test_calibration_block_empty_stats_returns_no_history_message() -> None:
    assert "No settled" in _build_calibration_block({})
    assert "No settled" in _build_calibration_block({"total_evaluated": 0})


def test_calibration_block_formats_accuracy_and_confidence() -> None:
    stats = {
        "total_evaluated": 20,
        "correct_count": 13,
        "avg_probability": 0.72,
        "avg_brier_score": 0.2345,
    }
    block = _build_calibration_block(stats)
    assert "20" in block
    assert "65.0%" in block   # 13/20 * 100
    assert "72.0%" in block   # avg_probability * 100
    assert "0.2345" in block


def test_calibration_block_handles_missing_optional_fields() -> None:
    stats = {"total_evaluated": 5, "correct_count": 3, "avg_probability": None, "avg_brier_score": None}
    block = _build_calibration_block(stats)
    assert "60.0%" in block   # 3/5 * 100
    assert "avg stated confidence" not in block
    assert "Brier" not in block


# --- calibration grounding ---

# --- _compute_macro_yield_curve_slope ---

def test_yield_curve_slope_computed_correctly() -> None:
    rows = [
        {"series_id": "DGS10", "value": "4.50", "observation_date": "2026-04-30"},
        {"series_id": "DGS2",  "value": "4.20", "observation_date": "2026-04-30"},
    ]
    result = _compute_macro_yield_curve_slope(rows)
    assert result is not None
    assert result["series_id"] == "YIELD_CURVE_SLOPE"
    assert abs(float(result["value"]) - 0.30) < 1e-9


def test_yield_curve_slope_missing_leg_returns_none() -> None:
    rows = [{"series_id": "DGS10", "value": "4.50", "observation_date": "2026-04-30"}]
    assert _compute_macro_yield_curve_slope(rows) is None


def test_yield_curve_slope_grounded_in_evidence() -> None:
    rows = [
        {"series_id": "DGS10", "value": "4.50", "observation_date": "2026-04-30"},
        {"series_id": "DGS2",  "value": "4.20", "observation_date": "2026-04-30"},
        {"series_id": "YIELD_CURVE_SLOPE", "value": "0.3000", "observation_date": "2026-04-30",
         "series_name": "Yield Curve Slope (10Y − 2Y)", "units": "%", "source_name": "computed"},
    ]
    evidence = "The yield curve slope is 0.30 percentage points, indicating a mildly positive term premium."
    assert not _check_evidence_grounding(evidence, _empty_snapshot(), macro_rows=rows)


def test_calibration_values_are_grounded_and_do_not_flag() -> None:
    calibration_stats = {
        "total_evaluated": 20,
        "correct_count": 13,
        "avg_probability": 0.72,
        "avg_brier_score": 0.2345,
    }
    # LLM quotes calibration-derived numbers in evidence
    evidence = "Recent accuracy for this target is 65.0% over 20 settled predictions."
    assert not _check_evidence_grounding(
        evidence, _empty_snapshot(), calibration_stats=calibration_stats
    )
