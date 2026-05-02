"""
Glassnode on-chain data connector.

The project task calls this module `glasschain`, but the external source is
Glassnode's REST API. We ingest selected crypto on-chain metrics as structured
macro records so they can flow through the existing normalization, feature, and
LLM prompt paths without a new storage table.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import (
    acquire_job_lock,
    increment_attempt,
    release_job_lock,
    send_to_dead_letter,
)
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

BASE_URL = "https://api.glassnode.com"
DEFAULT_INTERVAL = "24h"
DEFAULT_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class GlassnodeMetric:
    series_id: str
    name: str
    subtype: str
    path: str
    assets: tuple[str, ...]
    units: str
    currency: str | None = None


TRACKED_METRICS: tuple[GlassnodeMetric, ...] = (
    GlassnodeMetric(
        series_id="GLASSNODE_EXCHANGE_NETFLOW_NATIVE",
        name="Exchange Netflow Volume",
        subtype="onchain_exchange_netflow",
        path="/v1/metrics/transactions/transfers_volume_exchanges_net",
        assets=("BTC", "ETH"),
        units="native",
        currency="NATIVE",
    ),
    GlassnodeMetric(
        series_id="GLASSNODE_MINERS_OUTFLOW_MULTIPLE",
        name="Miner Outflow Multiple",
        subtype="onchain_miner_outflow",
        path="/v1/metrics/mining/miners_outflow_multiple",
        assets=("BTC",),
        units="ratio",
    ),
    GlassnodeMetric(
        series_id="GLASSNODE_LTH_NET_CHANGE_NATIVE",
        name="Long-Term Holder Position Change",
        subtype="onchain_lth_behavior",
        path="/v1/metrics/supply/lth_net_change",
        assets=("BTC",),
        units="native",
        currency="NATIVE",
    ),
)


def build_series_id(metric: GlassnodeMetric, asset: str) -> str:
    return f"{metric.series_id}_{asset}"


class GlasschainConnector(BaseConnector):
    source_name = "glassnode"
    category = "macro"
    base_url = BASE_URL
    auth_type = "api_key"
    trust_level = "unverified"
    rate_limit_per_minute = 30

    def _api_key(self) -> str:
        key = settings.glassnode_api_key
        if not key or key.startswith("REPLACE") or key.startswith("#"):
            raise RuntimeError("GLASSNODE_API_KEY is not set in .env")
        return key

    async def _fetch_metric(
        self,
        metric: GlassnodeMetric,
        *,
        asset: str,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        interval: str = DEFAULT_INTERVAL,
    ) -> list[dict]:
        until = get_utc_now()
        since = until - timedelta(days=lookback_days)
        params = {
            "api_key": self._api_key(),
            "a": asset,
            "s": int(since.timestamp()),
            "u": int(until.timestamp()),
            "i": interval,
            "f": "json",
        }
        if metric.currency is not None:
            params["c"] = metric.currency

        resp = await fetch_with_retry(
            f"{BASE_URL}{metric.path}",
            params=params,
            max_attempts=3,
        )
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"Glassnode error for {metric.series_id}/{asset}: {body['error']}")
        if not isinstance(body, list):
            raise RuntimeError(f"Glassnode returned unexpected payload for {metric.series_id}/{asset}")

        rows: list[dict] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            ts = item.get("t")
            value = item.get("v")
            if ts in (None, "") or value in (None, "", []):
                continue
            if isinstance(value, bool) or isinstance(value, (dict, list)):
                continue
            try:
                ts_int = int(ts)
                value_float = float(value)
            except (TypeError, ValueError):
                continue
            rows.append({"timestamp": ts_int, "value": value_float})
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
            for metric_index, metric in enumerate(TRACKED_METRICS):
                for asset_index, asset in enumerate(metric.assets):
                    if metric_index > 0 or asset_index > 0:
                        await asyncio.sleep(1)

                    log.info("glassnode_fetch_start", series_id=metric.series_id, asset=asset)
                    try:
                        rows = await self._fetch_metric(metric, asset=asset)
                    except Exception as exc:
                        log.error(
                            "glassnode_fetch_failed",
                            series_id=metric.series_id,
                            asset=asset,
                            error=str(exc),
                        )
                        continue

                    async with pool.acquire() as conn:
                        for row in rows:
                            obs_dt = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc)
                            obs_date = obs_dt.strftime("%Y-%m-%d")
                            series_id = build_series_id(metric, asset)
                            external_id = f"{series_id}::{obs_date}"
                            raw_payload = {
                                "series_id": series_id,
                                "series_name": metric.name,
                                "subtype": metric.subtype,
                                "asset": asset,
                                "frequency": DEFAULT_INTERVAL,
                                "observation_date": obs_date,
                                "value": row["value"],
                                "units": metric.units,
                                "applies_to_asset_type": "crypto",
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
                                source_recorded_at=obs_dt,
                                released_at=obs_dt,
                                record_version=plan.record_version,
                                prior_record_id=plan.prior_record_id,
                                checksum=plan.checksum,
                            )
                            total_new += 1

                    log.info(
                        "glassnode_metric_done",
                        series_id=metric.series_id,
                        asset=asset,
                        fetched=len(rows),
                        total_new=total_new,
                    )

            await release_job_lock(job_id, "succeeded")
            log.info("glassnode_run_complete", total_new=total_new)

        except Exception as exc:
            log.error("glassnode_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
