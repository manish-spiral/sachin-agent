"""Human review of scored jobs. Every status write goes through transition()."""

from __future__ import annotations

import re

from sqlmodel import col, select

from shared.db.engine import get_session
from shared.schemas.job import Job, JobStatus
from shared.schemas.job_transitions import transition
from shared.schemas.score import Dimension, StoredScore


class JobNotFound(LookupError):
    pass


def review_queue(limit: int = 20, min_score: float | None = None) -> list[Job]:
    """Highest-scored jobs still awaiting a decision."""
    query = select(Job).where(Job.status == JobStatus.SCORED, col(Job.duplicate_of).is_(None))
    if min_score is not None:
        query = query.where(col(Job.score) >= min_score)
    query = query.order_by(col(Job.score).desc(), col(Job.posted_at).desc()).limit(limit)
    with get_session() as session:
        return list(session.exec(query).all())


def find_job(id_prefix: str) -> Job:
    with get_session() as session:
        matches = list(
            session.exec(select(Job).where(col(Job.id).startswith(id_prefix)).limit(2)).all()
        )
    if len(matches) != 1:
        raise JobNotFound(
            f"no job matches id {id_prefix!r}" if not matches else f"id {id_prefix!r} is ambiguous"
        )
    return matches[0]


def decide(job_id: str, to: JobStatus) -> Job:
    """Approve or reject. Raises IllegalTransition for e.g. an already-applied job."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise JobNotFound(job_id)
        transition(job, to)
        session.add(job)
        session.commit()
        return job


def format_job(job: Job, *, show_score: bool = True) -> str:
    lines = [
        f"[{job.id[:10]}] {job.title} @ {job.company}",
        f"  {job.location_raw or 'location not stated'}"
        f"{' (remote)' if job.remote else ''}  {job.url}",
    ]
    if not show_score or not job.score_breakdown:
        return "\n".join(lines)

    stored = StoredScore.model_validate(job.score_breakdown)
    lines.insert(0, f"{stored.overall:.2f}/5")
    if stored.breakdown is None:
        lines.append(f"  prefilter: {stored.prefilter_reason}")
        return "\n".join(lines)
    for dim in Dimension:
        d = getattr(stored.breakdown, dim)
        cites = f"  [{', '.join(d.evidence)}]" if d.evidence else ""
        lines.append(f"  {dim.value:<15} {d.score}  {d.reason}{cites}")
    if stored.citation_issues:
        lines.append(f"  ⚠ cites facts that don't exist: {', '.join(stored.citation_issues)}")
    lines.append(f"  → {stored.breakdown.summary}")
    return "\n".join(lines)


# The scraper stores a normalized company key, but exclusion lists are typed by
# hand ("Globex Inc."); both sides drop legal suffixes before comparing.
_LEGAL_SUFFIXES = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|ag|sa|bv|pvt)\b\.?", re.I)


def _company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _LEGAL_SUFFIXES.sub(" ", name.lower()))


def _excluded(job: Job, companies: list[str], title_keywords: list[str]) -> str | None:
    company = _company_key(job.company)
    if any(company and company == _company_key(name) for name in companies):
        return f"company {job.company!r} excluded"
    title = job.title.lower()
    for keyword in title_keywords:
        if keyword.strip() and keyword.lower() in title:
            return f"title has excluded keyword {keyword!r}"
    return None


def auto_approve(
    min_score: float, *, limit: int, exclude_companies: list[str], exclude_title_keywords: list[str]
) -> list[Job]:
    """Policy approval: scored jobs at or above `min_score`, best first, minus
    exclusions. Submission still pauses for a human (constraint 10)."""
    approved: list[Job] = []
    for job in review_queue(limit=limit * 4, min_score=min_score):
        if len(approved) >= limit:
            break
        if _excluded(job, exclude_companies, exclude_title_keywords):
            continue
        approved.append(decide(job.id, JobStatus.APPROVED))
    return approved


def approved_without_documents(limit: int) -> list[Job]:
    from shared.schemas.document import DocumentKind, TailoredDocument

    with get_session() as session:
        has_resume = select(TailoredDocument.job_id).where(TailoredDocument.kind == DocumentKind.RESUME)
        query = (
            select(Job)
            .where(Job.status == JobStatus.APPROVED, col(Job.id).not_in(has_resume))
            .order_by(col(Job.score).desc())
            .limit(limit)
        )
        return list(session.exec(query).all())
