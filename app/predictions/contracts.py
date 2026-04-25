from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DirectionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal["up", "down", "neutral"]
    metric: Literal["price_return", "absolute_price", "relative_to_benchmark"]
    threshold: float | None = None
    unit: Literal["fraction", "percent", "usd"]

    @model_validator(mode="after")
    def validate_threshold(self) -> "DirectionRule":
        if self.direction == "neutral":
            if self.threshold is not None:
                raise ValueError("neutral targets must not set a threshold")
            return self

        if self.threshold is None:
            raise ValueError("directional targets must set a threshold")
        return self


class SettlementRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["continuous", "trading_day_close"]
    horizon: Literal["next_n_bars", "end_of_day", "wall_clock_hours"]
    n: int = Field(gt=0)
    calendar: Literal["NYSE", "CME", "LSE", "none"]


class FeatureValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_key: str
    feature_type: Literal["numeric", "text", "boolean", "json"]
    numeric_value: float | None = None
    text_value: str | None = None
    boolean_value: bool | None = None
    json_value: dict | list | None = None
    available_at: datetime
    source_record_ids: list[UUID] = Field(default_factory=list)


class FeatureSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    asset_id: UUID
    asset_symbol: str
    as_of_at: datetime
    feature_set_name: str
    values: list[FeatureValue]


class PredictionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    asset_type: Literal["crypto", "equity", "commodity", "forex"]
    target_metric: str
    horizon_hours: int = Field(gt=0)
    direction_rule: DirectionRule
    settlement_rule: SettlementRule
    asset_id: UUID | None = None
    is_active: bool = True


class PredictionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PredictionTarget
    asset_id: UUID
    asset_symbol: str
    asset_type: Literal["crypto", "equity", "commodity", "forex"]
    feature_snapshot: FeatureSnapshot
    model_version_id: UUID
    prompt_version_id: UUID | None = None
    probability: float = Field(ge=0.0, le=1.0)
    evidence_summary: str = Field(min_length=1, max_length=1000)
    predicted_outcome: str
    prediction_mode: Literal["live", "backtest"]
    created_at: datetime
    correlation_id: UUID
    rationale: dict = Field(default_factory=dict)
    hallucination_risk: bool = False
    probability_extreme_flag: bool = False
    context_compressed: bool = False
    backtest_run_id: UUID | None = None
    claim_type: Literal["correlation", "causal_hypothesis"] = "correlation"

    @field_validator("evidence_summary")
    @classmethod
    def strip_evidence_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("evidence_summary must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> "PredictionInput":
        if self.feature_snapshot.asset_id != self.asset_id:
            raise ValueError("feature_snapshot.asset_id must match asset_id")
        return self

    @property
    def horizon_end_at(self) -> datetime:
        return self.created_at + timedelta(hours=self.target.horizon_hours)

