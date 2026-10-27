from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import select

from shared.db.engine import get_session
from shared.db.upsert import upsert_job
from shared.schemas.job import Job, JobStatus, make_job_id


def _mk_job(**overrides) -> Job:
    now = datetime.now(UTC)
    base: dict = dict(
        id=make_job_id("gh:acme", "1"),
        source="gh:acme", external_id="1",
        title="Engineer", company="Acme",
        description_raw="old desc",
        first_seen_at=now, last_seen_at=now,
    )
    base.update(overrides)
    return Job(**base)


def test_make_job_id_is_deterministic_and_unique():
    assert make_job_id("s", "1") == make_job_id("s", "1")
    assert make_job_id("s", "1") != make_job_id("s", "2")
    assert make_job_id("a", "1") != make_job_id("b", "1")
    assert len(make_job_id("s", "1")) == 64   # sha256 hex


def test_upsert_inserts_new(fresh_db):
    j = _mk_job()
    with get_session() as s:
        result = upsert_job(s, j)
    assert result.title == "Engineer"
    with get_session() as s:
        count = len(s.scalars(select(Job)).all())
    assert count == 1


def test_upsert_updates_mutable_fields(fresh_db):
    j1 = _mk_job(title="Engineer", description_raw="v1")
    with get_session() as s:
        upsert_job(s, j1)

    j2 = _mk_job(title="Senior Engineer", description_raw="v2")
    with get_session() as s:
        result = upsert_job(s, j2)

    assert result.title == "Senior Engineer"
    assert result.description_raw == "v2"


def test_upsert_preserves_first_seen(fresh_db):
    j1 = _mk_job()
    with get_session() as s:
        upsert_job(s, j1)
    original_first_seen = j1.first_seen_at

    j2 = _mk_job()
    with get_session() as s:
        result = upsert_job(s, j2)

    assert result.first_seen_at == original_first_seen


def test_upsert_preserves_user_state(fresh_db):
    j = _mk_job()
    with get_session() as s:
        upsert_job(s, j)

    # Simulate matcher + user approval
    with get_session() as s:
        stored = s.scalars(select(Job).where(Job.id == j.id)).one()
        stored.status = JobStatus.APPROVED
        stored.score = 4.5
        s.add(stored)
        s.commit()

    # Re-scrape produces a fresh Job with default status
    with get_session() as s:
        result = upsert_job(s, _mk_job())

    assert result.status == JobStatus.APPROVED
    assert result.score == 4.5


def test_upsert_is_idempotent(fresh_db):
    j = _mk_job()
    for _ in range(5):
        with get_session() as s:
            upsert_job(s, j)
    with get_session() as s:
        count = len(s.scalars(select(Job)).all())
    assert count == 1