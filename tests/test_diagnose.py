"""Phase 11: every failure is captured and classified into the taxonomy."""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from applier.runner import run_application
from sandbox.cli import app as sandbox_cli
from sandbox.diagnose import Diagnosis, diagnose_pending, rule_diagnosis
from shared.db.cli import app as db_cli
from shared.db.engine import get_session
from shared.schemas.failure import FailureClass, FailureEvent
from tests.applier_helpers import JOB_ID, Browser, Scripted

F = FailureClass


def _event(message: str, *, phase: str = "fetch", module: str = "scraper", error_type: str = "Exception",
           emitted: FailureClass | None = F.UNKNOWN, snapshot_id: str | None = None) -> FailureEvent:
    return FailureEvent(id=str(uuid.uuid4()), source_module=module, phase=phase, error_type=error_type,
                        error_message=message, failure_class=emitted, snapshot_id=snapshot_id)


def _store(*events: FailureEvent) -> None:
    with get_session() as s:
        for e in events:
            s.add(e)
        s.commit()


def _all() -> list[FailureEvent]:
    with get_session() as s:
        return list(s.exec(select(FailureEvent)).all())


@pytest.mark.parametrize(("event", "expected"), [
    (_event("Error code: 429 - Rate limit reached ... tokens per day (TPD)"), F.RATE_LIMIT),
    (_event("input#first_name: css='input#first_name' matched nothing", error_type="SelectorNotFound"),
     F.SELECTOR_DRIFT),
    (_event("no adapter for https://jobs.example.com/1", phase="select_adapter"), F.UNSEEN_SITE),
    (_event("Error code: 400 - Failed to validate JSON. Please adjust your prompt."), F.SEMANTIC_MISMATCH),
    (_event("HTTP 503 from https://boards-api.greenhouse.io/v1/boards/x/jobs"), F.TRANSIENT),
    (_event("HTTP transport error: ConnectError"), F.TRANSIENT),
    (_event("'title'", phase="normalize", error_type="KeyError"), F.SCHEMA_DRIFT),
    (_event("HTTP 404 from https://api.lever.co/v0/postings/gone"), F.UNSEEN_SITE),
    (_event("captcha: challenge widget visible", phase="pause_captcha", module="applier"), F.CAPTCHA),
    (_event("HTTP 403 from https://x", phase="submit"), F.AUTH_WALL),
])
def test_rules(event, expected):
    diagnosis, _ = rule_diagnosis(event)  # type: ignore[misc]
    assert diagnosis.failure_class == expected


def test_unmatched_events_have_no_rule():
    assert rule_diagnosis(_event("something odd happened", phase="fill", module="applier")) is None


class _LLM:
    def __init__(self, result: Diagnosis | Exception) -> None:
        self.result, self.prompts = result, []

    def model_name(self, task) -> str:
        return "fake:diagnose"

    def __call__(self, task, system, user, model_cls):
        self.prompts.append(user)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_diagnose_pending_rules_then_llm_then_idempotent(fresh_db):
    _store(_event("Error code: 400 - Failed to validate JSON", emitted=F.TRANSIENT),  # emitter guessed wrong
           _event("Error code: 429 - rate limit", emitted=F.RATE_LIMIT),
           _event("Element is obscured by a modal wizard step 2 of 3", phase="fill", module="applier"))

    rules_only = diagnose_pending(llm=None)
    assert dict(rules_only.by_method) == {"rule": 2} and rules_only.left == 1 and rules_only.reclassified == 1
    fixed = next(e for e in _all() if "validate JSON" in e.error_message)
    assert fixed.failure_class == F.SEMANTIC_MISMATCH and fixed.diagnosis["emitted_class"] == "transient"

    llm = _LLM(Diagnosis(failure_class=F.NOVEL_INTERACTION, confidence=0.7, evidence="multi-step wizard"))
    report = diagnose_pending(llm=llm)  # type: ignore[arg-type]
    assert dict(report.by_method) == {"llm": 1}
    wizard = next(e for e in _all() if "wizard" in e.error_message)
    assert wizard.failure_class == F.NOVEL_INTERACTION
    assert wizard.diagnosis["model"] == "fake:diagnose" and wizard.diagnosis["confidence"] == 0.7
    assert all(e.diagnosed_at for e in _all())

    again = diagnose_pending(llm=llm)  # type: ignore[arg-type]
    assert sum(again.by_method.values()) == 0 and len(llm.prompts) == 1


def test_llm_failure_leaves_event_for_next_run(fresh_db):
    _store(_event("weird", phase="fill", module="applier"))
    report = diagnose_pending(llm=_LLM(RuntimeError("quota")))  # type: ignore[arg-type]
    assert report.left == 1 and _all()[0].diagnosed_at is None


def test_llm_sees_snapshot_evidence(applicant):
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant))
    from shared.schemas.snapshot import SnapshotIndex

    with get_session() as s:
        snap = s.exec(select(SnapshotIndex)).first()
    assert snap is not None
    _store(_event("odd", phase="fill", module="applier", snapshot_id=snap.id))
    llm = _LLM(Diagnosis(failure_class=F.UNKNOWN, confidence=0.2, evidence="?"))
    diagnose_pending(llm=llm)  # type: ignore[arg-type]
    assert "aria:" in llm.prompts[0] and "First Name" in llm.prompts[0]


def test_escalating_pauses_are_failure_events(applicant):
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant))  # pauses on consent
    [event] = _all()
    assert (event.phase, event.failure_class, event.source_module) == ("pause_consent", F.CONSENT, "applier")
    assert event.snapshot_id is not None
    diagnose_pending(llm=None)
    assert _all()[0].diagnosis["confidence"] == 1.0


def test_stats_and_diagnose_cli(fresh_db):
    _store(_event("Error code: 429 - rate limit", module="matcher", phase="score"),
           _event("Error code: 429 - rate limit", module="matcher", phase="score"),
           _event("mystery", phase="fill", module="applier"))
    runner = CliRunner()
    before = runner.invoke(db_cli, ["failures", "stats"]).output
    assert "failures: 3  (undiagnosed: 3)" in before and "unknown?" in before

    out = runner.invoke(sandbox_cli, ["diagnose", "--rules-only"]).output
    assert "still undiagnosed: 1" in out
    after = runner.invoke(db_cli, ["failures", "stats"]).output
    assert "rate_limit " in after and "matcher/score" in after and "(undiagnosed: 1)" in after
    assert "rate_limit" in runner.invoke(db_cli, ["failures", "list"]).output


def test_rows_from_before_the_diagnosis_column(fresh_db):
    """Live data had diagnosis=NULL (column added by migration); seen in the real DB."""
    from sqlalchemy import text

    _store(_event("Error code: 429 - rate limit"))
    with get_session() as s:
        s.exec(text("UPDATE failures SET diagnosis = NULL"))  # type: ignore[call-overload]
        s.commit()
    assert dict(diagnose_pending(llm=None).by_method) == {"rule": 1}
    assert "rate_limit" in CliRunner().invoke(db_cli, ["failures", "list"]).output
