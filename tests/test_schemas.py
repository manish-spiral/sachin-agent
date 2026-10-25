"""Smoke: schemas instantiate and round-trip through SQLite."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import select

from shared.db.engine import get_session
from shared.schemas import Application, Job, JobStatus


def _mk_job(**overrides) -> Job:
    now = datetime.now(UTC)
    base = dict(
        id="abc123",
        source="greenhouse:acme",
        external_id="999",
        title="Software Engineer",
        company="Acme",
        location_raw="Remote",
        description_raw="Build things.",
        url="https://example.com/jobs/999",
        first_seen_at=now,
        last_seen_at=now,
    )
    base.update(overrides)
    return Job(**base)


def test_job_defaults():
    j = _mk_job()
    assert j.status == JobStatus.NEW
    assert j.skills == []
    assert j.score is None


def test_job_roundtrip(fresh_db):
    j = _mk_job(skills=["python", "sql"])
    with get_session() as s:
        s.add(j)
        s.commit()

    with get_session() as s:
        loaded = s.exec(select(Job).where(Job.id == "abc123")).one()

    assert loaded.title == "Software Engineer"
    assert loaded.skills == ["python", "sql"]
    assert loaded.status == JobStatus.NEW


def test_application_roundtrip(fresh_db):
    j = _mk_job()
    with get_session() as s:
        s.add(j)
        s.commit()
        job_id = j.id

    app = Application(id="app1", job_id=job_id)
    with get_session() as s:
        s.add(app)
        s.commit()

    with get_session() as s:
        loaded = s.exec(select(Application).where(Application.id == "app1")).one()

    assert loaded.job_id == "abc123"
    assert loaded.status.value == "pending"