"""Tests for _check_evidence_grounding — specifically the macro_rows path."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.predictions.llm_engine import _check_evidence_grounding
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
