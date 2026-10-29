"""Phase 9: pipeline orchestration, caps, kill switch, summaries."""

from __future__ import annotations

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from orchestrator.cli import app as cli
from orchestrator.pipeline import Stage, StageResult, build_stages, run_pipeline, subprocess_runner
from shared.config import Settings, get_settings
from shared.db.engine import get_session
from shared.db.upsert import upsert_job
from shared.killswitch import is_killed, kill_file
from shared.schemas.job import Job, JobStatus, make_job_id
from shared.schemas.pipeline_run import PipelineRun, RunStatus


@pytest.fixture(autouse=True)
def _no_kill(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    kill_file().unlink(missing_ok=True)
    yield
    kill_file().unlink(missing_ok=True)


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr("orchestrator.pipeline.notify", lambda text, **kw: messages.append(text) or True)
    return messages


def _job(ext: str, *, status: JobStatus = JobStatus.NEW, score: float | None = None,
         company: str = "Acme", title: str = "Backend Engineer") -> Job:
    return Job(id=make_job_id("greenhouse:acme", ext), source="greenhouse:acme", external_id=ext,
               title=title, company=company, company_norm=company.lower(), status=status, score=score)


def _stages(*names: str) -> list[Stage]:
    return [Stage(n, ["true"]) for n in names]


def test_stage_plan_follows_settings():
    names = [s.name for s in build_stages(Settings())]
    assert names == ["scrape", "score", "tailor", "apply"]  # no auto-approval by default

    s = Settings(auto_approve_min_score=4.0, exclude_companies=["Globex"], exclude_title_keywords=["Manager"])
    approve = next(st for st in build_stages(s, scrape=False) if st.name == "approve")
    assert [st.name for st in build_stages(s, scrape=False)] == ["score", "approve", "tailor", "apply"]
    assert approve.argv[-6:] == ["--limit", "5", "--exclude-company", "Globex", "--exclude-title", "Manager"]
    # Every stage runs a module's own CLI in a subprocess, never imported code.
    assert {tuple(st.argv[1:3]) for st in build_stages(s)} == {
        ("-m", "scraper.cli"), ("-m", "matcher.cli"), ("-m", "applier.cli")}


def test_run_succeeds_records_stages_and_summarizes(fresh_db, sent):
    def runner(stage: Stage) -> StageResult:
        if stage.name == "scrape":
            with get_session() as s:
                upsert_job(s, _job("1"))
        return StageResult(0, f"{stage.name} ok")

    run = run_pipeline(_stages("scrape", "score"), runner=runner)

    assert run.status == RunStatus.SUCCEEDED and run.finished_at is not None
    assert [st["name"] for st in run.stages] == ["scrape", "score"]
    assert run.summary["jobs_change"] == {"new": 1}
    with get_session() as s:
        stored = s.get(PipelineRun, run.id)
    assert stored is not None and stored.status == RunStatus.SUCCEEDED
    assert len(sent) == 1 and "succeeded" in sent[0] and "✓ scrape" in sent[0]


def test_partial_and_failed_runs(fresh_db, sent):
    partial = run_pipeline(_stages("a", "b"), runner=lambda st: StageResult(int(st.name == "a"), ""))
    failed = run_pipeline(_stages("a", "b"), runner=lambda st: StageResult(1, "boom"))
    assert (partial.status, failed.status) == (RunStatus.PARTIAL, RunStatus.FAILED)
    assert "✗ a" in sent[0]


def test_kill_switch_mid_run_skips_remaining_stages(fresh_db, sent):
    def runner(stage: Stage) -> StageResult:
        if stage.name == "score":
            kill_file().write_text("x")
        return StageResult(0, "")

    run = run_pipeline(_stages("scrape", "score", "tailor", "apply"), runner=runner)

    assert run.status == RunStatus.KILLED
    assert [st["exit_code"] for st in run.stages] == [0, 0, None, None]


def test_kill_switch_env_and_cli(fresh_db, monkeypatch):
    runner = CliRunner()
    assert not is_killed()
    runner.invoke(cli, ["kill"])
    assert is_killed()
    blocked = runner.invoke(cli, ["run-all", "--no-notify"])
    assert blocked.exit_code == 1 and "Kill switch is on" in blocked.output
    runner.invoke(cli, ["resume"])
    assert not is_killed()
    monkeypatch.setenv("KILL_SWITCH", "1")
    assert is_killed()


def test_real_subprocess_stage_uses_module_cli(fresh_db):
    from orchestrator.pipeline import _module

    result = subprocess_runner(Stage("sources", _module("scraper", "sources")))
    assert result.exit_code == 0 and "greenhouse" in result.output


def test_history_cli(fresh_db, sent):
    run_pipeline(_stages("score"), runner=lambda st: StageResult(0, ""))
    out = CliRunner().invoke(cli, ["history"]).output
    assert "succeeded" in out and "score:0" in out


# --- module-side policies the pipeline relies on ----------------------------------


def test_auto_approve_threshold_limit_and_exclusions(fresh_db):
    from matcher.review import auto_approve

    with get_session() as s:
        for ext, score, company, title in [("1", 4.8, "Acme", "Backend Engineer"),
                                           ("2", 4.6, "Globex Inc.", "Backend Engineer"),
                                           ("3", 4.5, "Acme", "Engineering Manager"),
                                           ("4", 4.2, "Acme", "Platform Engineer"),
                                           ("5", 4.1, "Acme", "Go Engineer"),
                                           ("6", 2.0, "Acme", "Data Engineer")]:
            upsert_job(s, _job(ext, status=JobStatus.SCORED, score=score, company=company, title=title))

    approved = auto_approve(4.0, limit=2, exclude_companies=["Globex"], exclude_title_keywords=["manager"])

    assert [j.title for j in approved] == ["Backend Engineer", "Platform Engineer"]
    with get_session() as s:
        statuses = {j.external_id: j.status for j in s.exec(select(Job)).all()}
    assert statuses["2"] == statuses["3"] == statuses["6"] == JobStatus.SCORED


def test_daily_cap(applicant, monkeypatch):
    from applier.cli import app as applier_cli
    from applier.runner import applications_today, approved_ready
    from shared.config import reset_settings

    assert applications_today() == 0
    assert [j.external_id for j in approved_ready(5)] == ["1"]
    monkeypatch.setenv("MAX_APPLICATIONS_PER_DAY", "0")
    reset_settings()
    try:
        assert get_settings().max_applications_per_day == 0
        out = CliRunner().invoke(applier_cli, ["run-approved"]).output
        assert "Daily cap reached" in out
    finally:
        monkeypatch.delenv("MAX_APPLICATIONS_PER_DAY")
        reset_settings()


def test_kill_switch_stops_applier_and_matcher(applicant):
    from applier.runner import ApplyError, make_application_id, run_application
    from matcher.scoring import score_pending_jobs
    from shared.schemas.application import Application, PauseReason
    from tests.applier_helpers import JOB_ID, Browser, Scripted

    def throw_switch(domain, prompt, options):
        kill_file().write_text("x")  # thrown while the form is being filled
        return options[0]

    result = run_application(JOB_ID, human=Scripted(choose=throw_switch, approve=True),
                             session_factory=Browser(applicant))
    assert result.pause_reason == PauseReason.KILL_SWITCH  # paused with state, not submitted
    with get_session() as s:
        assert s.get(Application, make_application_id(JOB_ID)).browser_state_path  # type: ignore[union-attr]
    with pytest.raises(ApplyError, match="kill switch"):
        run_application(JOB_ID, human=Scripted(approve=True), session_factory=Browser(applicant))

    with get_session() as s:
        upsert_job(s, _job("99"))
    from tests.test_matcher import FakeScorer

    report = score_pending_jobs(scorer=FakeScorer())
    assert report.stopped == "kill switch" and report.scored == 0
