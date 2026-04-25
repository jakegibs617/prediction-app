"""
NewsAPI connector — financial and macro news headlines.

Endpoint used:
  /v2/everything — keyword search across all indexed sources

Free (developer) tier: 100 requests/day, results up to 1 month old, max 100
articles per request. Schedule no more than once per hour to stay well under
the daily cap. We fetch the 50 most recent articles per query per run.

Articles produce raw_source_records with category='news'. The normalization
pipeline picks these up and runs LLM extraction (sentiment, entities, topics).
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import NamedTuple

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter

log = structlog.get_logger(__name__)

BASE_URL = "https://newsapi.org/v2"

# Query strings — kept focused to avoid burning daily quota on irrelevant articles
NEWS_QUERIES: list[str] = [
    "bitcoin OR ethereum OR cryptocurrency",
    '"S&P 500" OR "Federal Reserve" OR inflation OR "interest rates"',
    '"oil price" OR "crude oil" OR gold OR silver',
]


def _article_external_id(url: str) -> str:
    return "newsapi::" + hashlib.sha256(url.encode()).hexdigest()[:16]


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class NewsApiConnector(BaseConnector):
    source_name = "newsapi"
    category = "news"
    base_url = BASE_URL
    auth_type = "api_key"
    trust_level = "unverified"
    rate_limit_per_minute = 10  # conservative — free tier undocumented per-minute limit

    def _api_key(self) -> str:
        key = settings.news_api_key
        if not key or key.startswith("REPLACE"):
            raise RuntimeError("NEWS_API_KEY is not set in .env")
        return key

    async def _fetch_articles(self, query: str, page_size: int = 50) -> list[dict]:
        # The free-tier developer key silently returns 0 results when a `from`
        # date filter is supplied. Omit it and rely on URL-hash deduplication.
        resp = await fetch_with_retry(
            f"{BASE_URL}/everything",
            params={
                "q": query,
                "apiKey": self._api_key(),
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": page_size,
            },
            max_attempts=3,
        )
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"NewsAPI error: {data.get('message', data.get('status'))}")
        return data.get("articles", [])

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

            for i, query in enumerate(NEWS_QUERIES):
                if i > 0:
                    await asyncio.sleep(2)

                log.info("newsapi_fetch_start", query=query)
                try:
                    articles = await self._fetch_articles(query)
                except Exception as exc:
                    log.error("newsapi_fetch_failed", query=query, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    for article in articles:
                        url = article.get("url", "")
                        if not url:
                            continue

                        external_id = _article_external_id(url)
                        raw_payload = {
                            "url": url,
                            "title": article.get("title") or "",
                            "description": article.get("description") or "",
                            "content": article.get("content") or "",
                            "source_name": (article.get("source") or {}).get("name") or "",
                            "author": article.get("author") or "",
                            "publishedAt": article.get("publishedAt") or "",
                            "query": query,
                        }
                        write_plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if not write_plan.should_write:
                            continue

                        published_at = _parse_published_at(article.get("publishedAt"))

                        await self.write_raw_record(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                            source_recorded_at=published_at,
                            released_at=published_at,
                            published_at=published_at,
                            record_version=write_plan.record_version,
                            prior_record_id=write_plan.prior_record_id,
                            checksum=write_plan.checksum,
                        )
                        total_raw += 1

                log.info("newsapi_query_done", query=query, new_articles=total_raw)

            await release_job_lock(job_id, "succeeded")
            log.info("newsapi_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("newsapi_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
