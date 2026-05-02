"""
Cboe options sentiment connector.

Ingests broad put/call ratios from Cboe options market statistics plus a small
VIX term-structure snapshot from Cboe futures settlement prices and VIX index
history. The records are stored as macro observations so they flow through the
existing feature snapshot and prompt context paths.
"""
from __future__ import annotations

import asyncio
import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from io import StringIO

import structlog

from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import (
    acquire_job_lock,
    increment_attempt,
    release_job_lock,
    send_to_dead_letter,
)

log = structlog.get_logger(__name__)

BASE_URL = "https://www.cboe.com"
OPTIONS_MARKET_STATS_URL = f"{BASE_URL}/us/options/market_statistics/market/"
FUTURES_SETTLEMENT_URL = f"{BASE_URL}/markets/us/futures/market-statistics/settlement/futures/daily"
VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


@dataclass(frozen=True)
class MacroObservation:
    series_id: str
    series_name: str
    subtype: str
    observation_date: str
    value: float
    units: str
    raw_details: dict

    @property
    def observed_at(self) -> datetime:
        return datetime.strptime(self.observation_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class VixFutureSettlement:
    symbol: str
    expiration_date: str
    settlement_price: float
    discretionary_settlement: bool = False


def _to_float(value: object) -> float | None:
    if value in (None, "", "."):
        return None
    try:
        return float(str(value).replace(",", "").replace("*", "").strip())
    except (TypeError, ValueError):
        return None


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _parse_cboe_long_date(value: str) -> str:
    return datetime.strptime(value.strip(), "%A, %B %d, %Y").date().isoformat()


def parse_put_call_observations(html: str) -> list[MacroObservation]:
    text = re.sub(r"\s+", " ", _strip_html(html)).strip()
    date_match = re.search(r"Cboe Exchange Market Statistics for ([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})", text)
    if not date_match:
        raise RuntimeError("Could not find Cboe options market statistics date")
    observation_date = _parse_cboe_long_date(date_match.group(1))

    routes = (
        ("Total", "CBOE_PUT_CALL_TOTAL", "Cboe Total Options Put/Call Ratio", "options_put_call_total"),
        ("Index Options", "CBOE_PUT_CALL_INDEX", "Cboe Index Options Put/Call Ratio", "options_put_call_index"),
        ("Equity Options", "CBOE_PUT_CALL_EQUITY", "Cboe Equity Options Put/Call Ratio", "options_put_call_equity"),
    )
    observations: list[MacroObservation] = []
    for section, series_id, series_name, subtype in routes:
        match = re.search(
            rf"{re.escape(section)}\s+TIME CALLS PUTS TOTAL P/C RATIO\s+(.*?)(?=\s+(?:Index Options|Equity Options|ETF Options|ETP Options|#|©)|$)",
            text,
            flags=re.DOTALL,
        )
        if not match:
            continue

        rows = re.findall(
            r"(\d{1,2}:\d{2}\s+[AP]M)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([0-9.]+)",
            match.group(1),
        )
        if not rows:
            continue
        time_label, calls, puts, total, ratio = rows[-1]
        ratio_value = _to_float(ratio)
        if ratio_value is None:
            continue
        observations.append(
            MacroObservation(
                series_id=series_id,
                series_name=series_name,
                subtype=subtype,
                observation_date=observation_date,
                value=ratio_value,
                units="ratio",
                raw_details={
                    "time_label": time_label,
                    "calls": int(calls.replace(",", "")),
                    "puts": int(puts.replace(",", "")),
                    "total_contracts": int(total.replace(",", "")),
                },
            )
        )
    return observations


def parse_vix_history_latest(csv_text: str) -> MacroObservation:
    rows = list(csv.DictReader(StringIO(csv_text)))
    if not rows:
        raise RuntimeError("Cboe VIX history CSV is empty")

    latest = rows[-1]
    close = _to_float(latest.get("CLOSE"))
    if close is None:
        raise RuntimeError("Cboe VIX history CSV latest row has no close")
    obs_date = datetime.strptime(latest["DATE"], "%m/%d/%Y").date().isoformat()
    return MacroObservation(
        series_id="CBOE_VIX_SPOT_CLOSE",
        series_name="Cboe VIX Spot Close",
        subtype="vix_spot_close",
        observation_date=obs_date,
        value=close,
        units="index_points",
        raw_details={
            "open": _to_float(latest.get("OPEN")),
            "high": _to_float(latest.get("HIGH")),
            "low": _to_float(latest.get("LOW")),
            "close": close,
        },
    )


def parse_vix_futures_settlements(html: str) -> tuple[str, list[VixFutureSettlement]]:
    text = _strip_html(html)
    date_match = re.search(r"Settlement Prices for (\d{4}-\d{2}-\d{2})", text)
    if not date_match:
        raise RuntimeError("Could not find Cboe futures settlement date")
    observation_date = date_match.group(1)

    vx_match = re.search(
        r"VX - Cboe Volatility Index \(VX\) Futures\s+Symbol - Expiration Date Settlement Price\s+(.*?)(?=\s+####\s+VXM|\s+VXM -|\s+####|\s+Settlement Prices for|\s+©|$)",
        text,
        flags=re.DOTALL,
    )
    if not vx_match:
        raise RuntimeError("Could not find VX futures settlement table")

    settlements: list[VixFutureSettlement] = []
    for symbol, expiration, price_text in re.findall(
        r"\b(VX[0-9A-Z/]*)\s+-\s+(\d{4}-\d{2}-\d{2})\s+([0-9.]+\*?)",
        vx_match.group(1),
    ):
        price = _to_float(price_text)
        if price is None:
            continue
        settlements.append(
            VixFutureSettlement(
                symbol=symbol,
                expiration_date=expiration,
                settlement_price=price,
                discretionary_settlement=price_text.endswith("*"),
            )
        )
    return observation_date, settlements


def build_vix_term_structure_observations(
    *,
    observation_date: str,
    spot: MacroObservation,
    settlements: list[VixFutureSettlement],
) -> list[MacroObservation]:
    if not settlements:
        return []

    out: list[MacroObservation] = []
    front = settlements[0]
    out.append(
        MacroObservation(
            series_id="CBOE_VIX_FRONT_FUTURE_SETTLE",
            series_name="Cboe VIX Front Future Settlement",
            subtype="vix_front_future_settlement",
            observation_date=observation_date,
            value=front.settlement_price,
            units="index_points",
            raw_details={
                "symbol": front.symbol,
                "expiration_date": front.expiration_date,
                "discretionary_settlement": front.discretionary_settlement,
            },
        )
    )
    out.append(
        MacroObservation(
            series_id="CBOE_VIX_FRONT_PREMIUM_TO_SPOT",
            series_name="Cboe VIX Front Future Premium to Spot",
            subtype="vix_front_premium_to_spot",
            observation_date=max(observation_date, spot.observation_date),
            value=front.settlement_price - spot.value,
            units="index_points",
            raw_details={
                "front_future": front.symbol,
                "front_future_settlement": front.settlement_price,
                "spot_observation_date": spot.observation_date,
                "spot_close": spot.value,
            },
        )
    )

    if len(settlements) >= 2:
        second = settlements[1]
        out.append(
            MacroObservation(
                series_id="CBOE_VIX_SECOND_FUTURE_SETTLE",
                series_name="Cboe VIX Second Future Settlement",
                subtype="vix_second_future_settlement",
                observation_date=observation_date,
                value=second.settlement_price,
                units="index_points",
                raw_details={
                    "symbol": second.symbol,
                    "expiration_date": second.expiration_date,
                    "discretionary_settlement": second.discretionary_settlement,
                },
            )
        )
        out.append(
            MacroObservation(
                series_id="CBOE_VIX_1M_2M_FUTURES_SLOPE",
                series_name="Cboe VIX Front-to-Second Future Slope",
                subtype="vix_front_second_futures_slope",
                observation_date=observation_date,
                value=second.settlement_price - front.settlement_price,
                units="index_points",
                raw_details={
                    "front_future": front.symbol,
                    "front_future_settlement": front.settlement_price,
                    "second_future": second.symbol,
                    "second_future_settlement": second.settlement_price,
                },
            )
        )
    return out


class CboeOptionsConnector(BaseConnector):
    source_name = "cboe_options"
    category = "macro"
    base_url = BASE_URL
    auth_type = "none"
    trust_level = "verified"
    rate_limit_per_minute = 30

    async def _fetch_put_call_observations(self) -> list[MacroObservation]:
        resp = await fetch_with_retry(OPTIONS_MARKET_STATS_URL, max_attempts=3)
        return parse_put_call_observations(resp.text)

    async def _fetch_vix_spot(self) -> MacroObservation:
        resp = await fetch_with_retry(VIX_HISTORY_URL, max_attempts=3)
        return parse_vix_history_latest(resp.text)

    async def _fetch_vix_term_structure(self, spot: MacroObservation) -> list[MacroObservation]:
        resp = await fetch_with_retry(FUTURES_SETTLEMENT_URL, max_attempts=3)
        observation_date, settlements = parse_vix_futures_settlements(resp.text)
        return build_vix_term_structure_observations(
            observation_date=observation_date,
            spot=spot,
            settlements=settlements,
        )

    async def run(self) -> None:
        job_id = await acquire_job_lock("macro_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()
            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            observations: list[MacroObservation] = []
            observations.extend(await self._fetch_put_call_observations())
            await asyncio.sleep(1)
            spot = await self._fetch_vix_spot()
            observations.append(spot)
            await asyncio.sleep(1)
            observations.extend(await self._fetch_vix_term_structure(spot))

            total_new = 0
            async with pool.acquire() as conn:
                for observation in observations:
                    raw_payload = {
                        "series_id": observation.series_id,
                        "series_name": observation.series_name,
                        "subtype": observation.subtype,
                        "frequency": "daily",
                        "observation_date": observation.observation_date,
                        "value": observation.value,
                        "units": observation.units,
                        "applies_to_asset_type": "all",
                        **observation.raw_details,
                    }
                    external_id = f"{observation.series_id}::{observation.observation_date}"
                    plan = await self.plan_raw_record_write(
                        conn,
                        source_id=source_id,
                        external_id=external_id,
                        raw_payload=raw_payload,
                    )
                    if not plan.should_write:
                        continue

                    await self.write_raw_record(
                        conn,
                        source_id=source_id,
                        external_id=external_id,
                        raw_payload=raw_payload,
                        source_recorded_at=observation.observed_at,
                        released_at=observation.observed_at,
                        record_version=plan.record_version,
                        prior_record_id=plan.prior_record_id,
                        checksum=plan.checksum,
                    )
                    total_new += 1

            await release_job_lock(job_id, "succeeded")
            log.info("cboe_options_run_complete", fetched=len(observations), total_new=total_new)

        except Exception as exc:
            log.error("cboe_options_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
