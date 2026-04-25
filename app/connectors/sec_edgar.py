"""
SEC EDGAR connector — recent 8-K and 10-Q filings as corporate event signals.

API used: EDGAR Full-Text Search (EFTS)
  https://efts.sec.gov/LATEST/search-index

No API key required. The SEC requires a descriptive User-Agent header with a
contact email (see SEC_EDGAR_USER_AGENT in .env). Requests without it may be
rate-limited or blocked.

Rate limit: SEC guidelines ask for max 10 req/sec. We stay well under that.

8-K filings are material corporate events — earnings releases, M&A activity,
executive changes, Reg FD disclosures. These are strong signals for SPY/QQQ
predictions. 10-Q filings provide quarterly financial context.

Filing metadata is stored in raw_source_records with category='events'. The
normalization pipeline will run LLM extraction on them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal

import structlog

from app.config import settings
from app.connectors.base import BaseConnector, fetch_with_retry
from app.db.pool import get_pool
from app.ops.job_runs import acquire_job_lock, increment_attempt, release_job_lock, send_to_dead_letter
from app.utils.time import get_utc_now

log = structlog.get_logger(__name__)

EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"

# 8-K item codes → human-readable descriptions for the normalization LLM
_8K_ITEMS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition (Earnings)",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events Accelerating Repayment of Obligation",
    "2.05": "Cost Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting",
    "3.02": "Unregistered Sales of Equity Securities",
    "4.01": "Change in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Appointment of Directors or Principal Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.05": "Amendments to the Registrant's Code of Ethics",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

FormType = Literal["8-K", "10-Q", "10-K"]

FETCH_FORMS: list[FormType] = ["8-K", "10-Q"]


def _user_agent() -> str:
    ua = settings.sec_edgar_user_agent
    if not ua or ua.startswith("REPLACE"):
        raise RuntimeError(
            "SEC_EDGAR_USER_AGENT is not set in .env. "
            "Format: 'AppName/Version contact@email.com'"
        )
    return ua


def _describe_items(items: list[str] | None) -> str:
    if not items:
        return ""
    descriptions = [_8K_ITEMS.get(code, f"Item {code}") for code in items]
    return "; ".join(descriptions)


def _parse_display_name(display_names: list[str] | None) -> str:
    if not display_names:
        return "Unknown Company"
    # Format: "COMPANY NAME  (TICKER)  (CIK 0001234567)"
    raw = display_names[0]
    # Strip CIK and ticker suffixes
    for sep in ("  (CIK", " (CIK"):
        if sep in raw:
            raw = raw[:raw.index(sep)]
    return raw.strip()


def _build_description(source: dict) -> str:
    entity = _parse_display_name(source.get("display_names"))
    form_type = source.get("form", source.get("file_type", "8-K"))
    file_date = source.get("file_date", "")
    period = source.get("period_ending", "")
    items = source.get("items") or []
    biz_location = (source.get("biz_locations") or [""])[0]

    parts = [f"SEC Form {form_type} filed by {entity} on {file_date}."]
    if period:
        parts.append(f"Period of report: {period}.")
    if items:
        desc = _describe_items(items)
        if desc:
            parts.append(f"Filing items: {desc}.")
    if biz_location:
        parts.append(f"Business location: {biz_location}.")
    return " ".join(parts)


class SecEdgarConnector(BaseConnector):
    source_name = "sec_edgar"
    category = "events"
    base_url = EFTS_BASE
    auth_type = "none"
    trust_level = "unverified"
    rate_limit_per_minute = 30  # well under SEC's 10 req/sec guideline

    async def _fetch_filings(
        self,
        form_type: FormType,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        resp = await fetch_with_retry(
            EFTS_BASE,
            headers={"User-Agent": _user_agent(), "Accept": "application/json"},
            params={
                "q": "",
                "forms": form_type,
                "dateRange": "custom",
                "startdt": start_date,
                "enddt": end_date,
            },
            max_attempts=3,
        )
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        return [h["_source"] for h in hits if "_source" in h]

    async def run(self) -> None:
        job_id = await acquire_job_lock("events_ingest", source_name=self.source_name)
        if job_id is None:
            return

        try:
            await increment_attempt(job_id)
            pool = get_pool()

            async with pool.acquire() as conn:
                source_id = await self.get_or_create_source(conn)

            now = get_utc_now()
            # Fetch filings from the last 48 hours to avoid missing any across day boundaries
            start_date = (now - timedelta(hours=48)).strftime("%Y-%m-%d")
            end_date = now.strftime("%Y-%m-%d")

            total_raw = 0

            for i, form_type in enumerate(FETCH_FORMS):
                if i > 0:
                    await asyncio.sleep(2)

                log.info("edgar_fetch_start", form_type=form_type, start_date=start_date)
                try:
                    filings = await self._fetch_filings(form_type, start_date, end_date)

                except Exception as exc:
                    log.error("edgar_fetch_failed", form_type=form_type, error=str(exc))
                    continue

                async with pool.acquire() as conn:
                    for filing in filings:
                        # EDGAR EFTS uses 'adsh' for the accession/document number
                        adsh = filing.get("adsh", "")
                        if not adsh:
                            continue

                        external_id = f"{adsh}::{form_type}"

                        file_date_str = filing.get("file_date", "")
                        source_recorded_at = None
                        if file_date_str:
                            try:
                                source_recorded_at = datetime.strptime(
                                    file_date_str, "%Y-%m-%d"
                                ).replace(tzinfo=timezone.utc)
                            except ValueError:
                                pass

                        entity_name = _parse_display_name(filing.get("display_names"))
                        description = _build_description(filing)
                        raw_payload = {
                            "adsh": adsh,
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": file_date_str,
                            "period_ending": filing.get("period_ending", ""),
                            "items": filing.get("items") or [],
                            "biz_locations": filing.get("biz_locations") or [],
                            "inc_states": filing.get("inc_states") or [],
                            "file_num": (filing.get("file_num") or [""])[0],
                            "ciks": filing.get("ciks") or [],
                            "description": description,
                        }
                        write_plan = await self.plan_raw_record_write(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                        )
                        if not write_plan.should_write:
                            continue

                        await self.write_raw_record(
                            conn,
                            source_id=source_id,
                            external_id=external_id,
                            raw_payload=raw_payload,
                            source_recorded_at=source_recorded_at,
                            released_at=source_recorded_at,
                            published_at=source_recorded_at,
                            record_version=write_plan.record_version,
                            prior_record_id=write_plan.prior_record_id,
                            checksum=write_plan.checksum,
                        )
                        total_raw += 1

                log.info(
                    "edgar_form_done",
                    form_type=form_type,
                    new_records=total_raw,
                )

            await release_job_lock(job_id, "succeeded")
            log.info("edgar_run_complete", total_raw=total_raw)

        except Exception as exc:
            log.error("edgar_run_failed", error=str(exc), exc_info=True)
            await send_to_dead_letter(job_id, str(exc))
            raise
