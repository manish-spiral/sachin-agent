"""Remotive public API. No auth required.

ToS: at most ~4 requests a day, link back to Remotive and name it as source.
Boards are Remotive category slugs (e.g. "software-dev") or "all".
"""

from __future__ import annotations

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id


class RemotiveSource(BaseSource):
    name = "remotive"
    domain = "remotive.com"
    default_boards = ("software-dev",)
    dedup_rank = 1
    min_interval_hours = 6.0

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        params = {} if company == "all" else {"category": company}
        data = await self.get_json(client, "https://remotive.com/api/remote-jobs", params=params)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return [
            RawJob(
                source="remotive",
                company=(j.get("company_name") or "").strip(),
                external_id=str(j["id"]),
                payload=j,
            )
            for j in jobs
            if j.get("id") and j.get("title")
        ]

    def normalize(self, raw: RawJob) -> Job:
        j = raw.payload
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=j["title"].strip(),
            company=raw.company,
            location_raw=j.get("candidate_required_location") or "",
            remote=True,
            description_raw=strip_html(j.get("description")),
            url=j.get("url") or "",
            posted_at=parse_dt(j.get("publication_date")),
            skills=[str(t) for t in j.get("tags") or []],
        )
