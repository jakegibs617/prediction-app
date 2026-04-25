"""
LLM-based prediction engine.

Calls the primary model client with a structured prompt built from the feature
snapshot and recent high-severity events relevant to the target asset.  Falls
back gracefully — callers catch any exception and delegate to the heuristic.
"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.db.pool import get_pool
from app.model_client.base import ModelClient
from app.predictions.contracts import FeatureSnapshot, PredictionInput, PredictionTarget

log = structlog.get_logger(__name__)

LLM_MODEL_NAME = "llm-prediction-engine"
LLM_MODEL_VERSION = "v1.0"

# Asset-type keyword sets used to match events from normalized_events
_ASSET_TYPE_KEYWORDS: dict[str, set[str]] = {
    "crypto": {"crypto", "bitcoin", "btc", "ethereum", "eth", "digital asset"},
    "equity": {"equity", "stock", "s&p", "spy", "wall street", "nasdaq", "markets"},
    "commodity": {"oil", "crude", "wti", "brent", "energy", "commodity", "eia"},
    "forex": {"dollar", "forex", "currency", "fed", "interest rate"},
}

_SYSTEM_PROMPT = """\
You are a quantitative financial analyst generating probabilistic predictions.

Rules:
- You may ONLY reference numeric values that appear in the provided feature data. \
Do not invent or estimate any number not shown.
- State statistical associations honestly. Do NOT claim causation unless a natural \
experiment, IV, or event study is cited.
- Use claim_type = "correlation" for observational associations. \
Use "causal_hypothesis" only when temporal precedence plus a plausible mechanism is cited.
- probability must be in [0.05, 0.95]. Values outside this range indicate overconfidence.
- evidence_summary must be ≤ 200 words and must not contain markdown.
"""


class LLMPredictionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    probability: float = Field(ge=0.0, le=1.0)
    predicted_outcome: str = Field(min_length=1, max_length=100)
    evidence_summary: str = Field(min_length=10, max_length=1000)
    claim_type: str = Field(pattern="^(correlation|causal_hypothesis)$")
    hallucination_risk: bool = False


def _build_feature_block(snapshot: FeatureSnapshot) -> str:
    lines = [f"Asset: {snapshot.asset_symbol}", f"Snapshot as-of: {snapshot.as_of_at.isoformat()}"]
    for v in snapshot.values:
        if v.numeric_value is not None:
            lines.append(f"  {v.feature_key}: {v.numeric_value:.6f}")
        elif v.text_value:
            lines.append(f"  {v.feature_key}: {v.text_value[:200]}")
    return "\n".join(lines)


def _build_target_block(target: PredictionTarget) -> str:
    dr = target.direction_rule
    threshold_str = f"{dr.threshold:.2%}" if dr.threshold else "none"
    return (
        f"Target: {target.name}\n"
        f"Direction: {dr.direction} | Threshold: {threshold_str} | Metric: {dr.metric}\n"
        f"Horizon: {target.horizon_hours} hours"
    )


def _build_events_block(events: list[dict]) -> str:
    if not events:
        return "No high-relevance events found."
    lines = []
    for e in events[:10]:
        lines.append(
            f"[{e['event_type']}] {e['title']} "
            f"(sentiment={e['sentiment_score']}, severity={e['severity_score']})"
        )
    return "\n".join(lines)


def _numeric_variants(value: float) -> set[str]:
    """Generate the common ways an LLM might re-format a numeric value, so the
    grounding check tolerates equivalent representations."""
    variants: set[str] = set()
    abs_v = abs(value)
    # Plain decimal at several precisions
    for prec in (0, 1, 2, 3, 4, 5, 6):
        variants.add(f"{value:.{prec}f}")
    # Strip trailing zeros / trailing dot to mimic '0.02' / '2.5'
    base = f"{value:.6f}".rstrip("0").rstrip(".")
    if base:
        variants.add(base)
    # Percent forms (multiply by 100) — these are how the model talks about returns
    pct = value * 100
    for prec in (0, 1, 2, 3, 4):
        variants.add(f"{pct:.{prec}f}%")
        variants.add(f"{pct:.{prec}f}")
    # Strip trailing zeros on the percent form too
    pct_base = f"{pct:.6f}".rstrip("0").rstrip(".")
    if pct_base:
        variants.add(pct_base)
        variants.add(f"{pct_base}%")
    # If the value is small enough to be a fraction, also include the integer form
    if abs_v < 1000:
        variants.add(str(int(value)))
    return variants


def _check_evidence_grounding(
    evidence: str,
    snapshot: FeatureSnapshot,
    *,
    target=None,
    events: list[dict] | None = None,
) -> bool:
    """Flag if the evidence mentions numbers that aren't grounded in the
    inputs: feature snapshot, target rule, or the events block.

    Tolerant to the common ways an LLM re-formats numbers (different
    precisions, percent vs fraction, trailing zeros, etc.).
    """
    import re
    numeric_in_evidence = set(re.findall(r"-?\d+\.?\d*%?", evidence))
    grounded: set[str] = set()

    # Feature snapshot values
    for v in snapshot.values:
        if v.numeric_value is not None:
            grounded.update(_numeric_variants(v.numeric_value))

    # Target horizon and threshold
    if target is not None:
        if getattr(target, "horizon_hours", None) is not None:
            grounded.add(str(int(target.horizon_hours)))
        dr = getattr(target, "direction_rule", None)
        if dr is not None and getattr(dr, "threshold", None) is not None:
            grounded.update(_numeric_variants(float(dr.threshold)))

    # Events block — sentiment_score, severity_score (they appear in the prompt)
    for e in (events or []):
        for key in ("sentiment_score", "severity_score"):
            val = e.get(key)
            if val is None:
                continue
            try:
                grounded.update(_numeric_variants(float(val)))
            except (TypeError, ValueError):
                continue

    # Anything 1-2 chars (e.g. '0', '5', '24') is too low-signal to flag
    suspicious = [
        n for n in numeric_in_evidence
        if len(n.lstrip("-")) > 2 and n not in grounded
    ]
    if suspicious:
        log.warning("evidence_grounding_suspicious", suspicious_values=suspicious)
        return True
    return False


async def fetch_relevant_events(asset_type: str, asset_symbol: str, limit: int = 15) -> list[dict]:
    keywords = _ASSET_TYPE_KEYWORDS.get(asset_type, set())
    symbol_root = asset_symbol.split("/")[0].lower()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_type, title, sentiment_score, severity_score, entity_data, event_occurred_at
            FROM ingestion.normalized_events
            WHERE severity_score::numeric > 0.1
            ORDER BY severity_score::numeric DESC, event_occurred_at DESC
            LIMIT $1
            """,
            limit * 3,
        )

    results = []
    for row in rows:
        title_lower = row["title"].lower()
        raw_entity = row["entity_data"] or {}
        if isinstance(raw_entity, str):
            try:
                raw_entity = json.loads(raw_entity)
            except Exception:
                raw_entity = {}
        asset_symbols_in_event = {
            a.get("symbol", "").lower()
            for a in raw_entity.get("assets", [])
        }
        is_relevant = (
            symbol_root in title_lower
            or symbol_root in asset_symbols_in_event
            or any(kw in title_lower for kw in keywords)
        )
        if is_relevant:
            results.append(dict(row))
        if len(results) >= limit:
            break

    return results


async def get_or_create_llm_model_version(model_name: str, model_version: str) -> UUID:
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM predictions.model_versions WHERE name = $1 AND version = $2",
            model_name,
            model_version,
        )
        if existing:
            return existing
        return await conn.fetchval(
            """
            INSERT INTO predictions.model_versions (name, version, model_type, config)
            VALUES ($1, $2, 'llm', $3)
            RETURNING id
            """,
            model_name,
            model_version,
            json.dumps({"engine": "llm", "version": model_version}),
        )


async def generate_llm_prediction_input(
    *,
    target: PredictionTarget,
    snapshot: FeatureSnapshot,
    asset_type: str,
    model_client: ModelClient,
    model_version_id: UUID,
    created_at: datetime,
    correlation_id: UUID,
    prediction_mode: str = "live",
) -> PredictionInput:
    events = await fetch_relevant_events(asset_type, snapshot.asset_symbol)

    feature_block = _build_feature_block(snapshot)
    target_block = _build_target_block(target)
    events_block = _build_events_block(events)

    user_message = (
        f"=== PREDICTION TARGET ===\n{target_block}\n\n"
        f"=== FEATURE DATA (use ONLY these numbers) ===\n{feature_block}\n\n"
        f"=== RELEVANT RECENT EVENTS ===\n{events_block}\n\n"
        "Generate a probability estimate and evidence summary for this prediction target."
    )

    output = await model_client.complete_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        output_schema=LLMPredictionOutput,
        max_retries=2,
    )

    probability = max(0.05, min(0.95, output.probability))
    probability_extreme = probability < 0.05 or probability > 0.95
    hallucination_risk = output.hallucination_risk or _check_evidence_grounding(
        output.evidence_summary, snapshot, target=target, events=events
    )

    log.info(
        "llm_prediction_generated",
        asset=snapshot.asset_symbol,
        target=target.name,
        probability=probability,
        claim_type=output.claim_type,
        hallucination_risk=hallucination_risk,
        event_count=len(events),
    )

    return PredictionInput(
        target=target,
        asset_id=snapshot.asset_id,
        asset_symbol=snapshot.asset_symbol,
        asset_type=asset_type,
        feature_snapshot=snapshot,
        model_version_id=model_version_id,
        probability=probability,
        evidence_summary=output.evidence_summary[:1000],
        predicted_outcome=output.predicted_outcome,
        prediction_mode=prediction_mode,
        created_at=created_at,
        correlation_id=correlation_id,
        rationale={
            "feature_count": len(snapshot.values),
            "macro_feature_count": sum(1 for v in snapshot.values if v.feature_key.startswith("macro__")),
            "event_count": len(events),
            "features_omitted": 0,
            "compression_type": None,
            "evidence_grounding_ok": not hallucination_risk,
            "model_name": LLM_MODEL_NAME,
            "model_version": LLM_MODEL_VERSION,
        },
        claim_type=output.claim_type,
        hallucination_risk=hallucination_risk,
        probability_extreme_flag=probability_extreme,
    )
