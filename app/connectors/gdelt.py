"""
GDELT DOC 2.0 connector — global news articles as event signals.

API used: GDELT Document 2.0 API (no API key required)
  https://api.gdeltproject.org/api/v2/doc/doc

Rate limit: 1 request per 5 seconds (enforced by GDELT). We space queries
with a 6-second sleep and set rate_limit_per_minute = 10.

GDELT monitors print/broadcast/web news worldwide in 65+ languages and
indexes articles every 15 minutes. ArtList mode returns article metadata
(url, title, domain, seendate, sourcecountry, language) for a query.

Articles are stored with category='news'. The normalization pipeline runs
LLM extraction for sentiment/entity metadata. Only English articles are
fetched to keep LLM prompts consistent.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import structlog

from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Queries targeting market-relevant global events
# Kept narrow to avoid burning the rate limit on irrelevant articles
GDELT_QUERIES: list[str] = [
    '(bitcoin OR ethereum OR cryptocurrency OR "digital asset")',
    '("stock market" OR "S&P 500" OR "Federal Reserve" OR "interest rates")',
    '("oil price" OR "crude oil" OR "natural gas" OR gold OR silver)',
    '("trade war" OR sanctions OR "supply chain" OR recession)',
]

# GDELT seendate format
_GDELT_DATE_FMT = "%Y%m%dT%H%M%SZ"


def _article_external_id(url: str) -> str:
    return "gdelt::" + hashlib.sha256(url.encode()).hexdigest()[:16]


def _parse_seendate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _GDELT_DATE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _build_description(article: dict) -> str:
    """Synthesize text for the normalization LLM from GDELT article metadata."""
    title = article.get("title", "")
    domain = article.get("domain", "")
    country = article.get("sourcecountry", "")
    seendate = article.get("seendate", "")

    parts = []
    if title:
        parts.append(title)
    if domain:
        parts.append(f"Source: {domain}.")
    if country:
        parts.append(f"Country: {country}.")
    if seendate:
        parts.append(f"Published: {seendate}.")
    return " ".join(parts)


class GdeltConnector(BaseConnector):
    source_name = "gdelt"
    category = "news"
    base_url = GDELT_DOC_URL
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 10  # GDELT enforces 1 req/5 sec

    async def _fetch_articles(self, query: str, max_records: int = 250) -> list[dict]:
        # base_delay=6 gives backoffs of 6s, 12s — safely above GDELT's 5s/req rule
        resp = await fetch_with_retry(
            GDELT_DOC_URL,
            params={
                "query": query,
                "mode": "ArtList",
                "maxrecords": str(max_records),
                "format": "json",
                "timespan": "24h",
                "sort": "DateDesc",
                "sourcelang": "english",
            },
            max_attempts=3,
            base_delay=6.0,
        )
        try:
            data = resp.json()
        except Exception:
            # GDELT returns plain-text on rate-limit or transient errors
            raise RuntimeError(f"GDELT non-JSON response: {resp.text[:120]}")
        return data.get("articles") or []

    async def run(self) -> None:
        job_id = await acquire_job_lock("news_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            total_raw = 0

            for i, query in enumerate(GDELT_QUERIES):
                if i > 0:
                    # GDELT rate limit: 1 req / 5 sec
                    await asyncio.sleep(6)

                log.info("gdelt_fetch_start", query=query)
                try:
                    articles = await self._fetch_articles(query)
                except Exception as exc:
                    log.error("gdelt_fetch_failed", query=query, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    for article in articles:
                        url = article.get("url", "")
                        if not url:
                            continue

                        # Skip non-English articles that slipped through
                        if article.get("language", "").lower() not in ("english", ""):
                            continue

                        external_id = _article_external_id(url)
                        raw_payload = {
                            "url": url,
                            "title": article.get("title", ""),
                            "domain": article.get("domain", ""),
                            "language": article.get("language", ""),
                            "sourcecountry": article.get("sourcecountry", ""),
                            "seendate": article.get("seendate", ""),
                            "socialimage": article.get("socialimage", ""),
                            "query": query,
                            "description": _build_description(article),
                        }
                        write_plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if not write_plan.should_write:
                            continue

                        seen_dt = _parse_seendate(article.get("seendate"))

                        await self.write_raw_record(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                            source_recorded_at=seen_dt,
                            released_at=seen_dt,
                            published_at=seen_dt,
                            record_version=write_plan.record_version,
                            prior_record_id=write_plan.prior_record_id,
                            checksum=write_plan.checksum,
                        )
                        total_raw += 1

                log.info("gdelt_query_done", query=query, new_articles=total_raw)

            await release_job_lock(job_id, "succeeded")
            log.info("gdelt_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("gdelt_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
