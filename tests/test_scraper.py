"""Phase 3: sources, retrying fetch, and runner failure isolation — all offline."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlmodel import select

from scraper.runner import run_all_sources
from scraper.sources import AshbySource, GreenhouseSource, HttpFetchError, LeverSource, RawJob
from scraper.sources.util import parse_dt, strip_html
from shared.db.engine import get_session
from shared.schemas.failure import FailureClass, FailureEvent
from shared.schemas.job import Job, make_job_id
from shared.schemas.source_health import SourceHealth

GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 101,
            "title": " Backend Engineer ",
            "location": {"name": "Remote - US"},
            "content": "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            "updated_at": "2026-01-02T03:04:05-05:00",
        },
        {"id": 102, "title": "Designer", "location": {"name": "Berlin"}},
    ]
}

LEVER_PAYLOAD = [
    {
        "id": "abc-123",
        "text": "Data Engineer",
        "categories": {"location": "Bangalore"},
        "workplaceType": "remote",
        "descriptionPlain": "Pipelines.",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "createdAt": 1767225600000,
    }
]

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "id": "uuid-1",
            "title": "ML Engineer",
            "location": "San Francisco",
            "isRemote": False,
            "descriptionHtml": "<p>Models</p>",
            "jobUrl": "https://jobs.ashbyhq.com/acme/uuid-1",
            "publishedAt": "2026-03-01T00:00:00.000+00:00",
        }
    ]
}


def _router(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def no_sleep(monkeypatch):
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("scraper.sources.base.asyncio.sleep", fake_sleep)
    return delays


# --- util ------------------------------------------------------------------


def test_parse_dt_formats():
    assert parse_dt("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_dt("Thu, 01 Jan 2026 00:00:00 GMT") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_dt(1767225600000) == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_dt(1767225600) == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_dt("not a date") is None
    assert parse_dt(None) is None


def test_strip_html():
    assert strip_html("<p>a &amp; b</p>\n<br/> c") == "a & b c"
    assert strip_html(None) == ""


# --- normalize -------------------------------------------------------------


def _raw(source: str, ext: str, payload: dict) -> RawJob:
    return RawJob(source=f"{source}:acme", company="acme", external_id=ext, payload=payload)


def test_greenhouse_normalize():
    job = GreenhouseSource().normalize(_raw("greenhouse", "101", GREENHOUSE_PAYLOAD["jobs"][0]))
    assert job.id == make_job_id("greenhouse:acme", "101")
    assert job.title == "Backend Engineer"
    assert job.remote is True
    assert job.posted_at is not None
    assert job.url.endswith("/101")


def test_lever_normalize_epoch_ms_and_workplace_type():
    job = LeverSource().normalize(_raw("lever", "abc-123", LEVER_PAYLOAD[0]))
    assert job.posted_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert job.remote is True
    assert job.location_raw == "Bangalore"


def test_ashby_normalize():
    job = AshbySource().normalize(_raw("ashby", "uuid-1", ASHBY_PAYLOAD["jobs"][0]))
    assert job.remote is False
    assert job.description_raw == "Models"


# --- get_json --------------------------------------------------------------


def test_get_json_retries_429_honouring_retry_after(no_sleep):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await GreenhouseSource().get_json(client, "https://x.test/")

    assert asyncio.run(go()) == {"ok": True}
    assert no_sleep == [7]


def test_get_json_caps_retry_after(no_sleep):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "99999"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await GreenhouseSource().get_json(client, "https://x.test/", max_retries=1)

    with pytest.raises(HttpFetchError) as exc_info:
        asyncio.run(go())
    assert exc_info.value.failure_class == FailureClass.RATE_LIMIT
    assert no_sleep == [120]


def test_get_json_404_is_not_retried(no_sleep):
    async def go():
        async with httpx.AsyncClient(transport=_router({})) as client:
            await GreenhouseSource().get_json(client, "https://x.test/")

    with pytest.raises(HttpFetchError) as exc_info:
        asyncio.run(go())
    assert exc_info.value.status_code == 404
    assert no_sleep == []


# --- runner ----------------------------------------------------------------


def _jobs() -> list[Job]:
    with get_session() as s:
        return list(s.exec(select(Job)).all())


def test_run_inserts_and_is_idempotent(fresh_db):
    transport = _router(
        {"https://boards-api.greenhouse.io/v1/boards/acme/": httpx.Response(200, json=GREENHOUSE_PAYLOAD)}
    )
    for _ in range(2):
        asyncio.run(run_all_sources(["greenhouse"], companies_override=["acme"], transport=transport))

    assert len(_jobs()) == 2
    with get_session() as s:
        health = s.get(SourceHealth, "greenhouse:acme")
    assert health is not None
    assert health.success_count == 2
    assert health.failure_count == 0


def test_bad_company_is_isolated(fresh_db, no_sleep):
    transport = _router(
        {"https://boards-api.greenhouse.io/v1/boards/acme/": httpx.Response(200, json=GREENHOUSE_PAYLOAD)}
    )
    asyncio.run(
        run_all_sources(["greenhouse"], companies_override=["nope", "acme"], transport=transport)
    )

    assert len(_jobs()) == 2
    with get_session() as s:
        failures = list(s.exec(select(FailureEvent)).all())
        health = s.get(SourceHealth, "greenhouse:nope")
    assert len(failures) == 1
    assert failures[0].phase == "fetch"
    assert failures[0].context == {"source": "greenhouse", "company": "nope"}
    assert health is not None
    assert health.failure_count == 1


def test_bad_job_is_isolated(fresh_db):
    payload = {"jobs": [*GREENHOUSE_PAYLOAD["jobs"], {"id": 103, "title": None, "location": "oops"}]}
    transport = _router(
        {"https://boards-api.greenhouse.io/v1/boards/acme/": httpx.Response(200, json=payload)}
    )
    asyncio.run(run_all_sources(["greenhouse"], companies_override=["acme"], transport=transport))

    assert len(_jobs()) == 2
    with get_session() as s:
        failures = list(s.exec(select(FailureEvent)).all())
    assert len(failures) == 1
    assert failures[0].phase == "normalize"
    assert failures[0].failure_class == FailureClass.SCHEMA_DRIFT
    assert failures[0].context["external_id"] == "103"
