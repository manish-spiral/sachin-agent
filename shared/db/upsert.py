"""Idempotent writes. The scraper (Phase 3) routes every job through upsert_job()."""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from shared.schemas.job import Job

# Whitelist of fields a re-scrape may overwrite. Everything else — status,
# score, score_breakdown, first_seen_at — is user/matcher state or history.
_UPDATE_FIELDS = (
    "last_seen_at",
    "title",
    "description_raw",
    "url",
    "posted_at",
    "salary_min",
    "salary_max",
    "salary_currency",
    "remote",
    "skills",
)


def upsert_job(session: Session, job: Job) -> Job:
    """Insert if new; on conflict update only `_UPDATE_FIELDS`."""
    stmt = sqlite_insert(Job).values(**job.model_dump(mode="python"))
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={name: getattr(job, name) for name in _UPDATE_FIELDS},
    )
    session.exec(stmt)
    session.commit()

    result = session.get(Job, job.id)
    assert result is not None, f"upsert failed for job {job.id}"
    return result
