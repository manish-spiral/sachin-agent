"""Phase 13: a broken selector is healed offline, promoted, used, and reversible."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from applier.adapters import adapter_for_url
from applier.adapters.spec import LocatorSpec, Strategy
from applier.adapters.versions import rollback, versions
from applier.cli import app as applier_cli
from applier.runner import run_application
from sandbox.healer import HealError, Proposals, ProposedLocator, deterministic_candidates, heal
from shared.db.engine import get_session
from shared.schemas.application import ApplicationStatus, PauseReason
from shared.schemas.failure import FailureClass, FailureEvent
from tests.applier_helpers import FIXTURE, JOB_ID, Browser, Scripted
from tests.test_replay import capture_forms

FORM = FIXTURE.read_text()
# The site renames its first-name input; the visible label stays "First Name*".
REDESIGN = FORM.replace('for="first_name"', 'for="firstName"').replace('id="first_name"', 'id="firstName"')
URL = "https://job-boards.greenhouse.io/acme/jobs/1"


def _drift_failures() -> list[FailureEvent]:
    with get_session() as s:
        return list(s.exec(select(FailureEvent).where(FailureEvent.failure_class == FailureClass.SELECTOR_DRIFT)))


def _break_live_run(applicant: Path) -> FailureEvent:
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant, REDESIGN))
    [event] = _drift_failures()
    return event


def test_candidates_come_from_the_failing_page():
    found = {(c.strategy, c.value) for c in deterministic_candidates(REDESIGN, "first_name")}
    assert (Strategy.CSS, "input#firstName") in found
    assert (Strategy.LABEL, "First Name") in found
    assert {(c.strategy, c.value) for c in deterministic_candidates(REDESIGN, "submit")} == {
        (Strategy.ROLE, "Submit application")}


def test_drift_is_healed_offline_promoted_used_and_rolled_back(applicant):
    capture_forms(applicant, 2)  # older captures of the domain, pre-redesign
    event = _break_live_run(applicant)
    assert event.context["intent"] == "form" and event.snapshot_id

    report = heal(event.id)

    assert report.broken == ["form", "first_name"]
    assert report.baseline_rate == 0.0 and report.final_rate == 1.0
    promoted = report.promoted
    assert promoted is not None and promoted.version == 2 and promoted.source == "synthesized"
    v1 = next(v for v in versions("greenhouse.io") if v.version == 1)
    assert promoted.parent_version_id == v1.id and not v1.is_active
    assert promoted.evidence["failure_id"] == event.id and promoted.evidence["trials"]
    # The label survives both layouts, so it beats the new id on overall evidence.
    healed = adapter_for_url(URL)
    assert healed is not None and healed.spec.version == 2
    assert healed.spec.fields[0].locator == LocatorSpec(strategy=Strategy.LABEL, value="First Name")

    # The healed adapter works on the redesigned site...
    ok = run_application(JOB_ID, human=Scripted(choose=lambda d, p, o: o[0]),
                         session_factory=Browser(applicant, REDESIGN))
    assert (ok.status, ok.pause_reason) == (ApplicationStatus.PAUSED, PauseReason.SUBMIT_APPROVAL)
    assert ok.adapter_version_id == "greenhouse.io@v2"
    with get_session() as s:
        assert s.get(FailureEvent, event.id).diagnosis["healed_by"] == promoted.id  # type: ignore[union-attr]

    # ...and rollback is one command.
    rollback("greenhouse.io", 1)
    assert adapter_for_url(URL).spec.version == 1  # type: ignore[union-attr]


def test_no_promotion_without_a_passing_candidate(applicant):
    hopeless = FORM.replace('id="first_name"', 'id="x9"').replace("First Name*", "Vorname*")
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant, hopeless))
    [event] = _drift_failures()

    report = heal(event.id)

    assert report.promoted is None and "no candidates" in report.reason
    assert [v.version for v in versions("greenhouse.io")] == [1]


class _LLM:
    def __init__(self, proposals: Proposals) -> None:
        self.proposals = proposals

    def __call__(self, task, system, user, model_cls):
        assert "accessibility tree" in user
        return self.proposals


def test_llm_candidates_are_tested_like_any_other(applicant):
    # Only the LLM can know "Vorname" means first name; replay still decides.
    renamed = FORM.replace('for="first_name"', 'for="vn"').replace('id="first_name"', 'id="vn"') \
                  .replace("First Name*", "Vorname*")
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant, renamed))
    [event] = _drift_failures()
    llm = _LLM(Proposals(candidates=[
        ProposedLocator(strategy=Strategy.LABEL, value="Vorname", role=None, rationale="German for first name"),
        ProposedLocator(strategy=Strategy.CSS, value="input#does-not-exist", role=None, rationale="guess"),
    ]))

    report = heal(event.id, llm=llm)  # type: ignore[arg-type]

    assert report.promoted is not None
    by_value = {t.locator.value: t for t in report.trials}
    assert by_value["Vorname"].eval_rate == 1.0 and by_value["input#does-not-exist"].eval_rate == 0.0


def test_refuses_non_drift_failures(applicant):
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant))  # consent pause
    with get_session() as s:
        event = s.exec(select(FailureEvent)).one()
    with pytest.raises(HealError, match="not a selector-drift"):
        heal(event.id)


def test_adapters_and_rollback_cli(applicant):
    event = _break_live_run(applicant)
    heal(event.id)
    runner = CliRunner()
    listing = runner.invoke(applier_cli, ["adapters", "greenhouse.io"]).output
    assert "* v2" in listing and "synthesized" in listing and "builtin" in listing
    assert "v1 is active" in runner.invoke(applier_cli, ["rollback", "greenhouse.io", "1"]).output
    assert runner.invoke(applier_cli, ["rollback", "greenhouse.io", "7"]).exit_code == 1
