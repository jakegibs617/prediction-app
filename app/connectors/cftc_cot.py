"""
CFTC Commitments of Traders connector.

Pulls weekly COT futures-only positioning from the CFTC Public Reporting
Environment. Commodity contracts use the Disaggregated report; CME bitcoin uses
the Traders in Financial Futures report.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

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

BASE_URL = "https://publicreporting.cftc.gov"
DISAGGREGATED_FUTURES_ONLY_DATASET = "72hh-3qpy"
TFF_FUTURES_ONLY_DATASET = "gpe5-46if"
DEFAULT_LIMIT = 12


@dataclass(frozen=True)
class CotRoute:
    series_id: str
    name: str
    subtype: str
    dataset_id: str
    contract_market_code: str
    long_field: str
    short_field: str
    trader_classification: str
    applies_to_asset_type: str
    units: str = "contracts"


TRACKED_ROUTES: tuple[CotRoute, ...] = (
    CotRoute(
        series_id="CFTC_COT_MANAGED_MONEY_NET_GOLD",
        name="CFTC COT Managed Money Net Positioning - Gold",
        subtype="cot_managed_money_net_positioning",
        dataset_id=DISAGGREGATED_FUTURES_ONLY_DATASET,
        contract_market_code="088691",
        long_field="m_money_positions_long_all",
        short_field="m_money_positions_short_all",
        trader_classification="managed_money",
        applies_to_asset_type="commodity",
    ),
    CotRoute(
        series_id="CFTC_COT_MANAGED_MONEY_NET_WTI_CRUDE",
        name="CFTC COT Managed Money Net Positioning - WTI Crude",
        subtype="cot_managed_money_net_positioning",
        dataset_id=DISAGGREGATED_FUTURES_ONLY_DATASET,
        contract_market_code="067651",
        long_field="m_money_positions_long_all",
        short_field="m_money_positions_short_all",
        trader_classification="managed_money",
        applies_to_asset_type="commodity",
    ),
    CotRoute(
        series_id="CFTC_COT_LEVERAGED_FUNDS_NET_BTC_CME",
        name="CFTC COT Leveraged Funds Net Positioning - CME Bitcoin",
        subtype="cot_leveraged_funds_net_positioning",
        dataset_id=TFF_FUTURES_ONLY_DATASET,
        contract_market_code="133741",
        long_field="lev_money_positions_long",
        short_field="lev_money_positions_short",
        trader_classification="leveraged_funds",
        applies_to_asset_type="crypto",
    ),
)


def _to_float(value: object) -> float | None:
    if value in (None, "", "."):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_report_date(value: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unsupported CFTC report date: {value!r}")


class CftcCotConnector(BaseConnector):
    source_name = "cftc_cot"
    category = "macro"
    base_url = BASE_URL
    auth_type = "none"
    trust_level = "verified"
    rate_limit_per_minute = 60

    async def _fetch_route(self, route: CotRoute, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
        url = f"{BASE_URL}/resource/{route.dataset_id}.json"
        params = {
            "$limit": limit,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "cftc_contract_market_code": route.contract_market_code,
        }
        resp = await fetch_with_retry(url, params=params, max_attempts=3)
        body = resp.json()
        if not isinstance(body, list):
            raise RuntimeError(f"CFTC COT returned unexpected payload for {route.series_id}")

        rows: list[dict] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            report_date = item.get("report_date_as_yyyy_mm_dd")
            long_value = _to_float(item.get(route.long_field))
            short_value = _to_float(item.get(route.short_field))
            open_interest = _to_float(item.get("open_interest_all"))
            if not report_date or long_value is None or short_value is None:
                continue

            net_value = long_value - short_value
            net_pct_open_interest = None
            if open_interest not in (None, 0):
                net_pct_open_interest = net_value / open_interest

            rows.append(
                {
                    "report_date": report_date,
                    "market_and_exchange_names": item.get("market_and_exchange_names"),
                    "contract_market_name": item.get("contract_market_name"),
                    "cftc_contract_market_code": item.get("cftc_contract_market_code"),
                    "commodity_name": item.get("commodity_name"),
                    "long_positions": long_value,
                    "short_positions": short_value,
                    "net_position": net_value,
                    "net_pct_open_interest": net_pct_open_interest,
                    "open_interest": open_interest,
                    "raw_row_id": item.get("id"),
                    "futonly_or_combined": item.get("futonly_or_combined"),
                }
            )
        return rows

    async def run(self) -> None:
        job_id = await acquire_job_lock("macro_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            total_new = 0
            for i, route in enumerate(TRACKED_ROUTES):
                if i > 0:
                    await asyncio.sleep(1)

                log.info("cftc_cot_fetch_start", series_id=route.series_id)
                try:
                    rows = await self._fetch_route(route)
                except Exception as exc:
                    log.error("cftc_cot_fetch_failed", series_id=route.series_id, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    for row in rows:
                        report_dt = _parse_report_date(row["report_date"])
                        report_date = report_dt.strftime("%Y-%m-%d")
                        external_id = f"{route.series_id}::{report_date}"
                        raw_payload = {
                            "series_id": route.series_id,
                            "series_name": route.name,
                            "subtype": route.subtype,
                            "frequency": "weekly",
                            "observation_date": report_date,
                            "value": row["net_position"],
                            "units": route.units,
                            "trader_classification": route.trader_classification,
                            "long_positions": row["long_positions"],
                            "short_positions": row["short_positions"],
                            "open_interest": row["open_interest"],
                            "net_pct_open_interest": row["net_pct_open_interest"],
                            "market_and_exchange_names": row["market_and_exchange_names"],
                            "contract_market_name": row["contract_market_name"],
                            "cftc_contract_market_code": row["cftc_contract_market_code"],
                            "commodity_name": row["commodity_name"],
                            "futonly_or_combined": row["futonly_or_combined"],
                            "applies_to_asset_type": route.applies_to_asset_type,
                            "source_dataset_id": route.dataset_id,
                            "raw_row_id": row["raw_row_id"],
                        }
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
                            source_recorded_at=report_dt,
                            released_at=report_dt,
                            record_version=plan.record_version,
                            prior_record_id=plan.prior_record_id,
                            checksum=plan.checksum,
                        )
                        total_new += 1

                log.info("cftc_cot_series_done", series_id=route.series_id, fetched=len(rows), total_new=total_new)

            await release_job_lock(job_id, "succeeded")
            log.info("cftc_cot_run_complete", total_new=total_new)

        except Exception as exc:
            log.error("cftc_cot_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
