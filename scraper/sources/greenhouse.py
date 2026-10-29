"""Greenhouse public job-board API. No auth required."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id


class GreenhouseSource(BaseSource):
    name = "greenhouse"
    domain = "greenhouse.io"

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        slug = quote(company.strip().lower())
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        data = await self.get_json(client, url, params={"content": "true"})
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return [
            RawJob(
                source=f"greenhouse:{company}",
                company=company,
                external_id=str(j.get("id", "")),
                payload=j,
            )
            for j in jobs
            if j.get("id") is not None
        ]

    def normalize(self, raw: RawJob) -> Job:
        j = raw.payload
        location = (j.get("location") or {}).get("name", "") or ""
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=(j.get("title") or "").strip(),
            company=raw.company,
            location_raw=location,
            remote="remote" in location.lower() or None,
            description_raw=strip_html(j.get("content")),
            url=j.get("absolute_url") or "",
            posted_at=parse_dt(j.get("updated_at") or j.get("created_at")),
            skills=[],
        )
