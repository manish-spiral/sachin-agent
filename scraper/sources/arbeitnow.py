"""Arbeitnow public job-board API (mostly Europe). No auth required.

ToS: free public API, "please do not abuse"; link back to Arbeitnow.
"""

from __future__ import annotations

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id

# 300 jobs per page, newest first; older pages rarely hold anything new.
MAX_PAGES = 3


class ArbeitnowSource(BaseSource):
    name = "arbeitnow"
    domain = "arbeitnow.com"
    default_boards = ("all",)
    dedup_rank = 1
    min_interval_hours = 1.0

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        jobs: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self.get_json(
                client, "https://www.arbeitnow.com/api/job-board-api", params={"page": page}
            )
            jobs.extend(data.get("data", []) if isinstance(data, dict) else [])
            if not (isinstance(data, dict) and (data.get("links") or {}).get("next")):
                break
        return [
            RawJob(
                source="arbeitnow",
                company=(j.get("company_name") or "").strip(),
                external_id=str(j["slug"]),
                payload=j,
            )
            for j in jobs
            if j.get("slug") and j.get("title")
        ]

    def normalize(self, raw: RawJob) -> Job:
        j = raw.payload
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=j["title"].strip(),
            company=raw.company,
            location_raw=j.get("location") or "",
            remote=bool(j.get("remote")),
            description_raw=strip_html(j.get("description")),
            url=j.get("url") or "",
            posted_at=parse_dt(j.get("created_at")),
            skills=[str(t) for t in j.get("tags") or []],
        )
