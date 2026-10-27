"""Idempotent writes. The scraper (Phase 3) routes every job through upsert_job()."""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from shared.schemas.job import Job

# Fields we deliberately do NOT overwrite on re-scrape — they represent state
# the user has asserted (via scoring / approval) or history we must preserve.
_PRESERVED_FIELDS = {
    "status",           # user decision (scored, approved, rejected, applied)
    "score",            # matcher output
    "score_breakdown",  # matcher reasoning
    "first_seen_at",    # historical fact
}


def upsert_job(session: Session, job: Job) -> Job:
    """
    Insert if new, update mutable fields if exists.

    Updated on conflict: title, description_raw, url, posted_at,
                        salary_min/max/currency, last_seen_at, remote, skills.

    Preserved on conflict: status, score, score_breakdown, first_seen_at.
    """
    values = job.model_dump(mode="python")
    for k in _PRESERVED_FIELDS:
        values.pop(k, None)  # exclude from INSERT-set on conflict path

    stmt = sqlite_insert(Job).values(**job.model_dump(mode="python"))
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "last_seen_at": job.last_seen_at,
            "title": job.title,
            "description_raw": job.description_raw,
            "url": job.url,
            "posted_at": job.posted_at,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_currency": job.salary_currency,
            "remote": job.remote,
            "skills": job.skills,
        },
    )
    session.exec(stmt)
    session.commit()

    result = session.get(Job, job.id)
    assert result is not None, f"upsert failed for job {job.id}"
    return result