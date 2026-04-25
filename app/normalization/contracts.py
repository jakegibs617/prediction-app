from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _coerce_confidence(v: Any) -> Any:
    """Local LLMs sometimes emit confidence: null. Coerce to neutral 0.5.
    Out-of-range numbers are clamped to [0.0, 1.0] so they don't fail Field(ge,le)."""
    if v is None:
        return 0.5
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


class OrgEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    _v_conf = field_validator("confidence", mode="before")(classmethod(lambda cls, v: _coerce_confidence(v)))


class PersonEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    role: str | None = None

    _v_conf = field_validator("confidence", mode="before")(classmethod(lambda cls, v: _coerce_confidence(v)))


class AssetEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str
    name: str = ""
    type: Literal["crypto", "equity", "commodity", "forex", "unknown"] = "unknown"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    _v_conf = field_validator("confidence", mode="before")(classmethod(lambda cls, v: _coerce_confidence(v)))


class PlaceEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    country_code: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    _v_conf = field_validator("confidence", mode="before")(classmethod(lambda cls, v: _coerce_confidence(v)))


_ASSET_TYPES = {"crypto", "equity", "commodity", "forex", "unknown"}


def _classify_loose_entity(item: dict) -> tuple[str, dict] | None:
    """Best-effort classify a free-form entity dict into one of the four buckets.
    Returns (bucket_name, normalized_fields) or None to drop it.
    """
    if not isinstance(item, dict):
        return None

    etype = (item.get("type") or "").lower()
    name = item.get("name") or item.get("symbol")
    symbol = item.get("symbol")
    confidence = item.get("confidence", 0.5)

    # Asset?  Has a symbol and the type is asset-like, OR explicitly tagged as asset.
    if symbol and (etype in _ASSET_TYPES or etype == "asset"):
        asset_type = etype if etype in _ASSET_TYPES else "unknown"
        return "assets", {
            "symbol": symbol,
            "name": item.get("name") or "",
            "type": asset_type,
            "confidence": confidence,
        }

    # Person?
    if etype in {"person", "people"} or item.get("role") is not None:
        if name:
            return "persons", {
                "name": name,
                "role": item.get("role"),
                "confidence": confidence,
            }

    # Place?
    if etype in {"place", "location", "city", "country", "region"} or item.get("country_code"):
        if name:
            return "places", {
                "name": name,
                "country_code": item.get("country_code"),
                "confidence": confidence,
            }

    # Default: organization.
    if name:
        return "orgs", {"name": name, "confidence": confidence}

    return None


class EntityData(BaseModel):
    model_config = ConfigDict(extra="ignore")
    orgs: list[OrgEntity] = Field(default_factory=list)
    persons: list[PersonEntity] = Field(default_factory=list)
    assets: list[AssetEntity] = Field(default_factory=list)
    places: list[PlaceEntity] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_loose_shapes(cls, data: Any) -> Any:
        """Tolerate the two common shapes local models emit instead of the strict dict:
          1) a flat list: [{name, type, ...}, ...]
          2) a list-of-dicts wrapping the buckets: [{"orgs": [...], "persons": [...]}]
        """
        if data is None:
            return {}
        if isinstance(data, dict):
            # Strict-dict path: still strip assets with null symbol (they are
            # really orgs/places that the model misclassified).
            assets = data.get("assets")
            if isinstance(assets, list):
                cleaned: list[dict] = []
                misplaced: list[dict] = []
                for a in assets:
                    if not isinstance(a, dict):
                        continue
                    if a.get("symbol"):
                        cleaned.append(a)
                    elif a.get("name"):
                        # No symbol, just a name -> it's an organization.
                        misplaced.append({
                            "name": a["name"],
                            "confidence": a.get("confidence", 0.5),
                        })
                data = {**data, "assets": cleaned}
                if misplaced:
                    existing_orgs = data.get("orgs")
                    if isinstance(existing_orgs, list):
                        data["orgs"] = existing_orgs + misplaced
                    else:
                        data["orgs"] = misplaced
            return data
        if isinstance(data, list):
            buckets: dict[str, list[dict]] = {
                "orgs": [],
                "persons": [],
                "assets": [],
                "places": [],
            }
            for item in data:
                if isinstance(item, dict) and any(
                    k in item for k in ("orgs", "persons", "assets", "places")
                ):
                    # Shape 2: merge bucketed dicts.
                    for k in buckets:
                        v = item.get(k)
                        if isinstance(v, list):
                            buckets[k].extend(x for x in v if isinstance(x, dict))
                else:
                    classified = _classify_loose_entity(item)
                    if classified is not None:
                        bucket, fields = classified
                        buckets[bucket].append(fields)
            return buckets
        return data


class ExtractionResult(BaseModel):
    """LLM-structured output schema for normalization extraction."""
    model_config = ConfigDict(extra="ignore")

    event_type: Literal["news", "economic_release", "geopolitical_event", "corporate_filing"]
    event_subtype: str | None = None

    @field_validator("event_type", mode="before")
    @classmethod
    def _coerce_event_type(cls, v: Any) -> Any:
        """Map free-form local-LLM event types into the four canonical buckets."""
        if not isinstance(v, str):
            return v
        s = v.strip().lower()
        if s in {"news", "economic_release", "geopolitical_event", "corporate_filing"}:
            return s
        # Natural disasters and physical events -> geopolitical_event
        if s in {"earthquake", "natural_disaster", "disaster", "weather", "storm", "flood", "wildfire", "hurricane", "tornado", "volcanic", "seismic"}:
            return "geopolitical_event"
        if s in {"market", "market_data", "price", "macro", "economic", "econ_release", "economic_data"}:
            return "economic_release"
        if s in {"sec_filing", "earnings", "corporate", "company_filing", "filing"}:
            return "corporate_filing"
        # Default fallback
        return "news"
    title: str
    summary: str
    sentiment_score: float = Field(
        ge=-1.0, le=1.0,
        description="-1.0 = very negative, 0.0 = neutral, 1.0 = very positive",
    )
    severity_score: float = Field(
        ge=0.0, le=1.0,
        description="0.0 = routine/minor, 1.0 = extreme market-moving event",
    )
    country_code: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code of primary geography, or null",
    )
    region: str | None = Field(
        default=None,
        description="Sub-national or broad region (e.g. 'North America'), or null",
    )
    entities: EntityData = Field(default_factory=EntityData)

    @field_validator("sentiment_score", "severity_score", mode="before")
    @classmethod
    def _default_null_scores(cls, v: Any, info: Any) -> Any:
        """Local LLMs sometimes emit null when they don't know — coerce to neutral default."""
        if v is None:
            return 0.0 if info.field_name == "sentiment_score" else 0.0
        return v

    @field_validator("title", "summary", mode="before")
    @classmethod
    def _default_null_text(cls, v: Any) -> Any:
        if v is None:
            return ""
        return v
