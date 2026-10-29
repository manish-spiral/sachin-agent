"""Ashby public job-board API. No auth required."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id


class AshbySource(BaseSource):
    name = "ashby"
    domain = "ashbyhq.com"

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        slug = quote(company.strip().lower())
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        data = await self.get_json(
            client, url, params={"includeCompensation": "true"}
        )
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        raws: list[RawJob] = []
        for j in jobs:
            ext = j.get("id") or j.get("jobId")
            if ext is None:
                continue
            raws.append(
                RawJob(
                    source=f"ashby:{company}",
                    company=company,
                    external_id=str(ext),
                    payload=j,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> Job:
        j = raw.payload
        location = j.get("location", "") or ""
        is_remote = j.get("isRemote")
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=(j.get("title") or "").strip(),
            company=raw.company,
            location_raw=location,
            remote=bool(is_remote) if is_remote is not None else ("remote" in location.lower()),
            description_raw=strip_html(
                j.get("descriptionHtml")
                or j.get("descriptionPlain")
                or j.get("description")
            ),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            posted_at=parse_dt(j.get("publishedAt")),
            skills=[],
        )
