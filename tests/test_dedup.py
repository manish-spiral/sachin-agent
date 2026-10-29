"""Phase 4: exact + fuzzy cross-source dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import select

from scraper.normalize import apply_normalization
from shared.db.dedup import dedup_jobs
from shared.db.engine import get_session
from shared.db.upsert import upsert_job
from shared.schemas.job import Job, make_job_id

PRIORITY = {"greenhouse": 0, "remoteok": 1}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _job(source: str, ext: str, title: str, company: str = "Acme", location: str = "Berlin",
         description: str = "", seen_offset: int = 0) -> Job:
    seen = T0 + timedelta(hours=seen_offset)
    return apply_normalization(
        Job(
            id=make_job_id(source, ext),
            source=source,
            external_id=ext,
            title=title,
            company=company,
            location_raw=location,
            description_raw=description or f"{source}-{ext}",
            first_seen_at=seen,
            last_seen_at=seen,
        )
    )


def _store(*jobs: Job) -> None:
    with get_session() as s:
        for job in jobs:
            upsert_job(s, job)


def _dedup():
    with get_session() as s:
        return dedup_jobs(s, priority=PRIORITY)


def _dup_map() -> dict[str, str | None]:
    with get_session() as s:
        return {j.id: j.duplicate_of for j in s.exec(select(Job)).all()}


def test_cross_source_fuzzy_duplicate_points_at_ats_copy(fresh_db):
    # The aggregator saw it first, but the direct ATS posting must win canonical.
    ats = _job("greenhouse:acme", "1", "Senior Backend Engineer", seen_offset=5)
    agg = _job("remoteok", "9", "Sr. Backend Engineer (Remote)", company="Acme Inc.")
    _store(ats, agg)

    report = _dedup()

    assert report.duplicates == 1
    assert _dup_map() == {ats.id: None, agg.id: ats.id}


def test_different_location_is_not_a_duplicate(fresh_db):
    _store(
        _job("greenhouse:acme", "1", "Backend Engineer", location="Berlin"),
        _job("remoteok", "9", "Backend Engineer", location="Bengaluru"),
    )
    assert _dedup().duplicates == 0


def test_unknown_location_matches(fresh_db):
    _store(
        _job("greenhouse:acme", "1", "Backend Engineer", location="Berlin"),
        _job("remoteok", "9", "Backend Engineer", location=""),
    )
    assert _dedup().duplicates == 1


def test_dissimilar_titles_are_not_duplicates(fresh_db):
    _store(
        _job("greenhouse:acme", "1", "Backend Engineer"),
        _job("remoteok", "9", "Frontend Engineer"),
    )
    assert _dedup().duplicates == 0


def test_different_companies_are_never_compared(fresh_db):
    _store(
        _job("greenhouse:acme", "1", "Backend Engineer"),
        _job("remoteok", "9", "Backend Engineer", company="Globex"),
    )
    assert _dedup().duplicates == 0


def test_same_source_requires_identical_text(fresh_db):
    a = _job("greenhouse:acme", "1", "Software Engineer", description="Team A")
    b = _job("greenhouse:acme", "2", "Software Engineer", description="Team B")
    c = _job("greenhouse:acme", "3", "Software Engineer", description="Team A", seen_offset=1)
    _store(a, b, c)

    _dedup()

    assert _dup_map() == {a.id: None, b.id: None, c.id: a.id}


def test_dedup_is_idempotent_and_survives_rescrape(fresh_db):
    ats = _job("greenhouse:acme", "1", "Backend Engineer")
    agg = _job("remoteok", "9", "Backend Engineer")
    _store(ats, agg)
    first = _dedup()
    second = _dedup()
    assert (first.duplicates, first.changed) == (1, 1)
    assert (second.duplicates, second.changed) == (1, 0)

    # A re-scrape builds a fresh Job (duplicate_of=None); upsert must not wipe it.
    _store(_job("remoteok", "9", "Backend Engineer"))
    assert _dup_map()[agg.id] == ats.id


def test_duplicate_flag_clears_when_no_longer_duplicate(fresh_db):
    ats = _job("greenhouse:acme", "1", "Backend Engineer")
    agg = _job("remoteok", "9", "Backend Engineer")
    _store(ats, agg)
    _dedup()

    _store(_job("remoteok", "9", "Data Scientist"))
    report = _dedup()

    assert report.duplicates == 0
    assert _dup_map()[agg.id] is None
