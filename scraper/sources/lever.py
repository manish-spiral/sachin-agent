"""Lever public job-board API. No auth required."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from scraper.sources.base import BaseSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.schemas.job import Job, make_job_id


class LeverSource(BaseSource):
    name = "lever"
    domain = "lever.co"

    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        slug = quote(company.strip().lower())
        url = f"https://api.lever.co/v0/postings/{slug}"
        data = await self.get_json(client, url, params={"mode": "json"})
        postings = data if isinstance(data, list) else []
        return [
            RawJob(
                source=f"lever:{company}",
                company=company,
                external_id=str(post.get("id", "")),
                payload=post,
            )
            for post in postings
            if post.get("id") is not None
        ]

    def normalize(self, raw: RawJob) -> Job:
        post = raw.payload
        categories = post.get("categories") or {}
        location = categories.get("location", "") or ""
        description = (
            post.get("descriptionPlain")
            or strip_html(post.get("description"))
            or ""
        )
        return Job(
            id=make_job_id(raw.source, raw.external_id),
            source=raw.source,
            external_id=raw.external_id,
            title=(post.get("text") or "").strip(),
            company=raw.company,
            location_raw=location,
            remote=_is_remote(post.get("workplaceType"), location),
            description_raw=description,
            url=post.get("hostedUrl") or "",
            posted_at=parse_dt(post.get("createdAt")),
            skills=[],
        )


def _is_remote(workplace_type: str | None, location: str) -> bool | None:
    if workplace_type:
        return workplace_type.lower() == "remote"
    return "remote" in location.lower() or None
