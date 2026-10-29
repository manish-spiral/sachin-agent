"""SmartRecruiters public posting API. No auth required.

The list endpoint has no description, so `enrich` fetches each posting's
detail. Company identifiers are case-sensitive ("ServiceNow", not "servicenow").
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id

PAGE_SIZE = 100
# Bounds a single board's cost: each posting is one more detail request.
MAX_POSTINGS = 1000
DETAIL_CONCURRENCY = 4
_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


class SmartRecruitersSource(BaseSource):
    name = "smartrecruiters"
    domain = "smartrecruiters.com"

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        slug = quote(company.strip())
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        postings: list[dict] = []
        while len(postings) < MAX_POSTINGS:
            data = await self.get_json(
                client, url, params={"limit": PAGE_SIZE, "offset": len(postings)}
            )
            page = data.get("content", []) if isinstance(data, dict) else []
            postings.extend(page)
            if len(page) < PAGE_SIZE or len(postings) >= data.get("totalFound", 0):
                break
        return [
            RawJob(
                source=f"smartrecruiters:{company}",
                company=(p.get("company") or {}).get("name") or company,
                external_id=str(p["id"]),
                payload=p,
            )
            for p in postings[:MAX_POSTINGS]
            if p.get("id")
        ]

    async def enrich(
        self, client: httpx.AsyncClient, raws: list[RawJob]
    ) -> tuple[list[RawJob], list[tuple[RawJob, Exception]]]:
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
        errors: list[tuple[RawJob, Exception]] = []

        async def one(raw: RawJob) -> None:
            ref = raw.payload.get("ref")
            if not ref:
                return
            async with semaphore:
                try:
                    raw.payload["detail"] = await self.get_json(client, ref)
                except Exception as exc:
                    errors.append((raw, exc))

        await asyncio.gather(*(one(raw) for raw in raws))
        return raws, errors

    def normalize(self, raw: RawJob) -> Job:
        p = raw.payload
        detail = p.get("detail") or {}
        location = detail.get("location") or p.get("location") or {}
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        description = " ".join(
            strip_html((sections.get(key) or {}).get("text")) for key in _SECTIONS
        ).strip()
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=(p.get("name") or "").strip(),
            company=raw.company,
            location_raw=location.get("fullLocation")
            or ", ".join(x for x in (location.get("city"), location.get("country")) if x),
            remote=location.get("remote"),
            description_raw=description,
            url=detail.get("postingUrl")
            or f"https://jobs.smartrecruiters.com/{quote(raw.company)}/{raw.external_id}",
            posted_at=parse_dt(p.get("releasedDate")),
        )
