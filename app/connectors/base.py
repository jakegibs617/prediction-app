import asyncio
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

import httpx
import structlog

log = structlog.get_logger(__name__)

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RawRecordWritePlan:
    should_write: bool
    record_version: int
    prior_record_id: UUID | None
    checksum: str


async def fetch_with_retry(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> httpx.Response:
    """GET with exponential backoff on 429 and 5xx. Raises on final failure."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers or {}, params=params or {})
            if resp.status_code not in _RETRY_STATUS_CODES:
                resp.raise_for_status()
                return resp
            log.warning(
                "fetch_retryable_status",
                url=url, status=resp.status_code, attempt=attempt,
            )
        except httpx.RequestError as exc:
            log.warning("fetch_request_error", url=url, attempt=attempt, error=str(exc))
            last_exc = exc

        if attempt < max_attempts:
            delay = base_delay * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

    raise RuntimeError(f"fetch_with_retry exhausted {max_attempts} attempts for {url}") from last_exc


def compute_checksum(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class BaseConnector(ABC):
    """
    All source connectors extend this. Each connector is responsible for:
    - Ensuring its ops.api_sources row exists (get_or_create_source)
    - Fetching raw records from the external API
    - Writing to ingestion.raw_source_records
    - Writing derived rows (price_bars, etc.) as needed
    """

    source_name: str
    category: str  # 'market_data' | 'news' | 'events' | 'macro'
    base_url: str
    auth_type: str = "none"
    trust_level: str = "unverified"
    rate_limit_per_minute: int | None = None

    async def get_or_create_source(self, conn) -> UUID:
        """Upsert this connector's ops.api_sources row and return its UUID."""
        row = await conn.fetchrow(
            "SELECT id FROM ops.api_sources WHERE name = $1",
            self.source_name,
        )
        if row:
            return row["id"]

        source_id = await conn.fetchval(
            """
            INSERT INTO ops.api_sources
                (name, category, base_url, auth_type, trust_level, rate_limit_per_minute)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            self.source_name,
            self.category,
            self.base_url,
            self.auth_type,
            self.trust_level,
            self.rate_limit_per_minute,
        )
        log.info("source_registered", source_name=self.source_name, source_id=str(source_id))
        return source_id

    async def check_duplicate(self, conn, source_id: UUID, external_id: str, version: int = 1) -> bool:
        exists = await conn.fetchval(
            """
            SELECT 1 FROM ingestion.raw_source_records
            WHERE source_id = $1 AND external_id = $2 AND record_version = $3
            """,
            source_id, external_id, version,
        )
        return bool(exists)

    async def plan_raw_record_write(
        self,
        conn,
        *,
        source_id: UUID,
        external_id: str,
        raw_payload: dict,
    ) -> RawRecordWritePlan:
        checksum = compute_checksum(raw_payload)
        latest = await conn.fetchrow(
            """
            SELECT id, record_version, checksum
            FROM ingestion.raw_source_records
            WHERE source_id = $1 AND external_id = $2
            ORDER BY record_version DESC
            LIMIT 1
            """,
            source_id,
            external_id,
        )
        if latest is None:
            return RawRecordWritePlan(
                should_write=True,
                record_version=1,
                prior_record_id=None,
                checksum=checksum,
            )

        if latest["checksum"] == checksum:
            return RawRecordWritePlan(
                should_write=False,
                record_version=latest["record_version"],
                prior_record_id=latest["id"],
                checksum=checksum,
            )

        return RawRecordWritePlan(
            should_write=True,
            record_version=latest["record_version"] + 1,
            prior_record_id=latest["id"],
            checksum=checksum,
        )

    async def write_raw_record(
        self,
        conn,
        *,
        source_id: UUID,
        external_id: str,
        raw_payload: dict,
        source_recorded_at=None,
        released_at=None,
        published_at=None,
        record_version: int = 1,
        prior_record_id: UUID | None = None,
        checksum: str | None = None,
    ) -> UUID:
        from app.utils.time import get_utc_now
        payload_checksum = checksum or compute_checksum(raw_payload)
        record_id = await conn.fetchval(
            """
            INSERT INTO ingestion.raw_source_records
                (source_id, external_id, record_version, source_recorded_at, released_at,
                 published_at, ingested_at, raw_payload, checksum, prior_record_id, validation_status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending')
            RETURNING id
            """,
            source_id, external_id, record_version,
            source_recorded_at, released_at, published_at,
            get_utc_now(), json.dumps(raw_payload), payload_checksum, prior_record_id,
        )
        return record_id

    @abstractmethod
    async def run(self) -> None:
        """Fetch and persist all records for one cron tick."""
