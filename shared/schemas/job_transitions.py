"""Legal state transitions for Job.status. Every write goes through transition()."""

from __future__ import annotations

from shared.schemas.job import Job, JobStatus

# The authoritative map. Add transitions here — never mutate status directly.
LEGAL_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.NEW:      {JobStatus.SCORED, JobStatus.REJECTED, JobStatus.FAILED},
    JobStatus.SCORED:   {JobStatus.APPROVED, JobStatus.REJECTED, JobStatus.FAILED},
    JobStatus.APPROVED: {JobStatus.APPLIED, JobStatus.FAILED},
    JobStatus.APPLIED:  set(),                             # terminal
    JobStatus.REJECTED: {JobStatus.NEW},                   # manual reset only
    JobStatus.FAILED:   {JobStatus.NEW, JobStatus.SCORED}, # retry
}


class IllegalTransition(ValueError):
    """Raised when a caller tries to move a job to an illegal status."""


def transition(job: Job, to: JobStatus) -> Job:
    """Validate and apply a status change. Raises IllegalTransition if illegal."""
    if job.status == to:
        return job

    allowed = LEGAL_TRANSITIONS.get(job.status, set())
    if to not in allowed:
        raise IllegalTransition(
            f"Job {job.id}: illegal transition {job.status.value} → {to.value}; "
            f"allowed from {job.status.value}: "
            f"{sorted(s.value for s in allowed) or 'none (terminal)'}"
        )

    job.status = to
    return job