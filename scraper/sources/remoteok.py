"""Remote OK public feed. No auth required.

ToS: link back to the Remote OK URL and name Remote OK as the source. We keep
their URL on Job.url and `remoteok` in Job.source, and never republish.
"""

from __future__ import annotations

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id


class RemoteOKSource(BaseSource):
    name = "remoteok"
    domain = "remoteok.com"
    default_boards = ("all",)
    dedup_rank = 1
    min_interval_hours = 1.0

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        params = {} if company == "all" else {"tag": company}
        data = await self.get_json(client, "https://remoteok.com/api", params=params)
        # The first element is the legal notice, not a job.
        jobs = [j for j in data if isinstance(j, dict) and j.get("id")] if isinstance(data, list) else []
        return [
            RawJob(
                # One source string for every board: the same posting found via
                # two tags must hash to the same job id.
                source="remoteok",
                company=(j.get("company") or "").strip(),
                external_id=str(j["id"]),
                payload=j,
            )
            for j in jobs
            if j.get("position")
        ]

    def normalize(self, raw: RawJob) -> Job:
        j = raw.payload
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=j["position"].strip(),
            company=raw.company,
            location_raw=j.get("location") or "",
            remote=True,
            description_raw=strip_html(j.get("description")),
            url=j.get("url") or "",
            posted_at=parse_dt(j.get("date")) or parse_dt(j.get("epoch")),
            salary_min=j.get("salary_min") or None,
            salary_max=j.get("salary_max") or None,
            salary_currency="USD" if j.get("salary_min") or j.get("salary_max") else None,
            skills=[str(t) for t in j.get("tags") or []],
        )
