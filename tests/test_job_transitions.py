from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shared.schemas.job import Job, JobStatus
from shared.schemas.job_transitions import IllegalTransition, transition


def _mk_job(status: JobStatus = JobStatus.NEW) -> Job:
    now = datetime.now(UTC)
    return Job(
        id="j1", source="s", external_id="1", title="t", company="c",
        status=status, first_seen_at=now, last_seen_at=now,
    )


def test_legal_new_to_scored():
    j = _mk_job(JobStatus.NEW)
    transition(j, JobStatus.SCORED)
    assert j.status == JobStatus.SCORED


def test_legal_scored_to_approved():
    j = _mk_job(JobStatus.SCORED)
    transition(j, JobStatus.APPROVED)
    assert j.status == JobStatus.APPROVED


def test_legal_approved_to_applied():
    j = _mk_job(JobStatus.APPROVED)
    transition(j, JobStatus.APPLIED)
    assert j.status == JobStatus.APPLIED


def test_applied_is_terminal():
    j = _mk_job(JobStatus.APPLIED)
    with pytest.raises(IllegalTransition, match="terminal"):
        transition(j, JobStatus.NEW)


def test_illegal_scored_to_applied():
    j = _mk_job(JobStatus.SCORED)
    with pytest.raises(IllegalTransition, match="scored → applied"):
        transition(j, JobStatus.APPLIED)


def test_noop_same_status_ok():
    j = _mk_job(JobStatus.NEW)
    transition(j, JobStatus.NEW)   # no error, no change
    assert j.status == JobStatus.NEW


def test_reject_then_reset():
    j = _mk_job(JobStatus.NEW)
    transition(j, JobStatus.REJECTED)
    transition(j, JobStatus.NEW)   # explicit reset is allowed
    assert j.status == JobStatus.NEW