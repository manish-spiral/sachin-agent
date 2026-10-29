"""Human review of scored jobs. Every status write goes through transition()."""

from __future__ import annotations

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
