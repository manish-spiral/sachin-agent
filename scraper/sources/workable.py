"""Workable public widget API. No auth required."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id


class WorkableSource(BaseSource):
    name = "workable"
    domain = "workable.com"

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        slug = quote(company.strip().lower())
        url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
        data = await self.get_json(client, url, params={"details": "true"})
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        display_name = (data.get("name") if isinstance(data, dict) else None) or company
        return [
            RawJob(
                source=f"workable:{company}",
                company=display_name,
                external_id=str(j["shortcode"]),
                payload=j,
            )
            for j in jobs
            if j.get("shortcode")
        ]

    def normalize(self, raw: RawJob) -> Job:
        j = raw.payload
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=(j.get("title") or "").strip(),
            company=raw.company,
            location_raw=_location(j),
            remote=bool(j.get("telecommuting")),
            description_raw=strip_html(j.get("description")),
            url=j.get("url") or j.get("shortlink") or "",
            posted_at=parse_dt(j.get("published_on") or j.get("created_at")),
        )


def _location(j: dict) -> str:
    places = j.get("locations") or [
        {"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}
    ]
    rendered = [
        ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
        for loc in places
        if not loc.get("hidden")
    ]
    return "; ".join(r for r in rendered if r)
