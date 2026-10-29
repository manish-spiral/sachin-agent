"""Phase 4: Workable, SmartRecruiters, RemoteOK, Remotive, Arbeitnow, plus
runner politeness and the health/stats CLI. All offline."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlmodel import select
from typer.testing import CliRunner

from scraper.cli import app
from scraper.runner import run_all_sources
from scraper.sources import (
    ArbeitnowSource,
    RemoteOKSource,
    RemotiveSource,
    SmartRecruitersSource,
    WorkableSource,
)
from shared.db.engine import get_session
from shared.schemas.failure import FailureEvent
from shared.schemas.job import Job

WORKABLE = {
    "name": "Hugging Face",
    "jobs": [
        {
            "shortcode": "81B4",
            "title": "ML Engineer - EMEA Remote",
            "telecommuting": True,
            "url": "https://apply.workable.com/j/81B4",
            "published_on": "2026-05-29",
            "locations": [
                {"city": "Paris", "region": "Île-de-France", "country": "France", "hidden": False},
                {"city": "Hidden", "country": "X", "hidden": True},
            ],
            "description": "<p>Models</p>",
        }
    ],
}

SR_LIST = "https://api.smartrecruiters.com/v1/companies/Acme/postings"
SR_DETAIL = "https://api.smartrecruiters.com/v1/companies/Acme/postings/"


def _sr_posting(i: int) -> dict:
    return {
        "id": str(i),
        "name": f"Engineer {i}",
        "company": {"identifier": "Acme", "name": "Acme Corp"},
        "releasedDate": "2026-10-29T15:22:56.603Z",
        "location": {"city": "Vienna", "country": "us"},
        "ref": f"{SR_DETAIL}{i}",
    }


def _sr_detail(i: int) -> dict:
    return {
        "postingUrl": f"https://jobs.smartrecruiters.com/Acme/{i}-engineer",
        "location": {"fullLocation": "Vienna, Virginia, United States", "remote": True},
        "jobAd": {
            "sections": {
                "companyDescription": {"text": "<p>We are Acme</p>"},
                "jobDescription": {"text": "<p>Build</p>"},
                "qualifications": {"text": "<p>Python</p>"},
            }
        },
    }


REMOTEOK = [
    {"last_updated": 1, "legal": "Link back to Remote OK"},
    {
        "id": "1137573",
        "position": "Account Executive",
        "company": "Huzzle",
        "date": "2026-10-26T08:00:12+00:00",
        "location": "",
        "description": "<p>Sell</p>",
        "url": "https://remoteOK.com/remote-jobs/1137573",
        "salary_min": 0,
        "salary_max": 0,
        "tags": ["saas", "exec"],
    },
]

REMOTIVE = {
    "0-legal-notice": "...",
    "jobs": [
        {
            "id": 2091135,
            "title": "Senior Back-end Developer",
            "company_name": "Lemon.io",
            "candidate_required_location": "Europe",
            "publication_date": "2026-10-27T12:56:13",
            "description": "<p>Code</p>",
            "url": "https://remotive.com/remote-jobs/2091135",
            "tags": ["python"],
        }
    ],
}


def _arbeitnow_page(page: int, has_next: bool) -> dict:
    return {
        "data": [
            {
                "slug": f"job-{page}",
                "title": "Growth Product Manager (f/m/d)",
                "company_name": "7NXT GmbH",
                "location": "Berlin",
                "remote": False,
                "description": "<h3>Hi</h3>",
                "url": f"https://www.arbeitnow.com/jobs/{page}",
                "tags": ["Product Management"],
                "created_at": 1793280666,
            }
        ],
        "links": {"next": "https://x/?page=2" if has_next else None},
    }


async def _fetch_all(source, transport: httpx.MockTransport, board: str):
    async with httpx.AsyncClient(transport=transport) as client:
        raws = await source.fetch(client, board)
        return await source.enrich(client, raws)


def test_workable():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=WORKABLE))
    raws, errors = asyncio.run(_fetch_all(WorkableSource(), transport, "huggingface"))
    assert errors == []
    job = WorkableSource().normalize(raws[0])
    assert job.source == "workable:huggingface"
    assert job.company == "Hugging Face"
    assert job.location_raw == "Paris, Île-de-France, France"
    assert job.remote is True
    assert job.posted_at == datetime(2026, 5, 29, tzinfo=UTC)


def test_smartrecruiters_paginates_and_enriches(monkeypatch):
    monkeypatch.setattr("scraper.sources.smartrecruiters.PAGE_SIZE", 2)
    postings = [_sr_posting(i) for i in range(3)]
    seen_offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(SR_DETAIL):
            i = int(url.rsplit("/", 1)[1])
            if i == 2:
                return httpx.Response(404)
            return httpx.Response(200, json=_sr_detail(i))
        offset = int(request.url.params["offset"])
        seen_offsets.append(str(offset))
        return httpx.Response(200, json={"totalFound": 3, "content": postings[offset : offset + 2]})

    raws, errors = asyncio.run(
        _fetch_all(SmartRecruitersSource(), httpx.MockTransport(handler), "Acme")
    )

    assert seen_offsets == ["0", "2"]
    assert len(raws) == 3
    assert [r.external_id for r, _ in errors] == ["2"]

    enriched = SmartRecruitersSource().normalize(raws[0])
    assert enriched.company == "Acme Corp"
    assert enriched.description_raw == "Build Python"
    assert enriched.remote is True
    assert enriched.url.endswith("/0-engineer")

    # A failed detail still yields a usable (description-less) job.
    bare = SmartRecruitersSource().normalize(raws[2])
    assert bare.description_raw == ""
    assert bare.location_raw == "Vienna, us"


def test_remoteok_skips_legal_notice_and_zero_salary():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=REMOTEOK))
    raws, _ = asyncio.run(_fetch_all(RemoteOKSource(), transport, "all"))
    assert len(raws) == 1
    job = RemoteOKSource().normalize(raws[0])
    assert job.source == "remoteok"
    assert job.remote is True
    assert job.salary_min is None
    assert job.salary_currency is None
    assert job.skills == ["saas", "exec"]


def test_remoteok_tag_board_sends_tag():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=REMOTEOK)

    raws, _ = asyncio.run(_fetch_all(RemoteOKSource(), httpx.MockTransport(handler), "python"))
    assert seen == ["https://remoteok.com/api?tag=python"]
    # Same posting from any tag hashes to the same id.
    assert raws[0].source == "remoteok"


def test_remotive():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=REMOTIVE)

    raws, _ = asyncio.run(_fetch_all(RemotiveSource(), httpx.MockTransport(handler), "software-dev"))
    assert seen == ["https://remotive.com/api/remote-jobs?category=software-dev"]
    job = RemotiveSource().normalize(raws[0])
    assert job.external_id == "2091135"
    assert job.company == "Lemon.io"
    assert job.posted_at is not None and job.posted_at.tzinfo is not None


def test_arbeitnow_follows_next_link_and_stops():
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(str(page))
        return httpx.Response(200, json=_arbeitnow_page(page, has_next=page < 2))

    raws, _ = asyncio.run(_fetch_all(ArbeitnowSource(), httpx.MockTransport(handler), "all"))
    assert pages == ["1", "2"]
    job = ArbeitnowSource().normalize(raws[0])
    assert job.remote is False
    assert job.posted_at == datetime.fromtimestamp(1793280666, tz=UTC)


# --- runner ----------------------------------------------------------------


def _remotive_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=REMOTIVE)

    return httpx.MockTransport(handler)


def test_runner_normalizes_and_respects_min_interval(fresh_db):
    calls: list[str] = []
    transport = _remotive_transport(calls)

    first = asyncio.run(run_all_sources(["remotive"], transport=transport))
    second = asyncio.run(run_all_sources(["remotive"], transport=transport))
    forced = asyncio.run(run_all_sources(["remotive"], transport=transport, force=True))

    assert len(calls) == 2
    assert first.processed == {"remotive:software-dev": 1}
    assert second.skipped_boards == ["remotive:software-dev"]
    assert forced.processed == {"remotive:software-dev": 1}

    with get_session() as s:
        job = s.exec(select(Job)).one()
    assert job.title_norm == "senior back end developer"
    assert job.company_norm == "lemonio"
    assert job.location_norm == "europe | remote"


def test_runner_records_enrich_failures(fresh_db):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(SR_DETAIL):
            return httpx.Response(404)
        return httpx.Response(200, json={"totalFound": 1, "content": [_sr_posting(0)]})

    report = asyncio.run(
        run_all_sources(
            ["smartrecruiters"], companies_override=["Acme"], transport=httpx.MockTransport(handler)
        )
    )

    assert report.processed == {"smartrecruiters:Acme": 1}
    with get_session() as s:
        failure = s.exec(select(FailureEvent)).one()
    assert failure.phase == "enrich"
    assert failure.context["external_id"] == "0"


# --- CLI -------------------------------------------------------------------


@pytest.fixture
def cli():
    return CliRunner()


def test_health_and_stats_cli(fresh_db, cli):
    calls: list[str] = []
    asyncio.run(run_all_sources(["remotive"], transport=_remotive_transport(calls)))

    health = cli.invoke(app, ["health"])
    assert health.exit_code == 0, health.output
    assert "remotive:software-dev" in health.output
    assert "100%" in health.output

    by_source = cli.invoke(app, ["health", "--by-source"])
    assert "remotive " in by_source.output

    stats = cli.invoke(app, ["stats"])
    assert stats.exit_code == 0, stats.output
    assert "total: 1  unique: 1  duplicates: 0" in stats.output
    assert "europe" in stats.output


def test_sources_cli_lists_all_eight(cli):
    result = cli.invoke(app, ["sources"])
    names = [line.split()[0] for line in result.output.splitlines() if line.strip()]
    assert len(names) == 8


def test_example_config_covers_every_source():
    from scraper.sources import SOURCE_REGISTRY

    with open("scraper/companies.example.json", encoding="utf-8") as fh:
        assert set(json.load(fh)) == set(SOURCE_REGISTRY)
