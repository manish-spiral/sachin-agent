"""Phase 12: adapters are judged offline against stored snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from applier.adapters import builtin_spec
from applier.adapters.form import FormAdapter
from applier.adapters.spec import LocatorSpec, Strategy
from applier.snapshot import SnapshotRecorder
from sandbox.cli import app as sandbox_cli
from sandbox.harness import form_snapshots, run_suite
from sandbox.replay import ReplaySession, replay
from sandbox.suites import ATS_FORM_SUITE
from shared.schemas.snapshot import SnapshotIndex
from tests.applier_helpers import FIXTURE, Browser

FORM = FIXTURE.read_text()


def capture_forms(tmp: Path, n: int = 5, *, domain: str = "job-boards.greenhouse.io") -> list[SnapshotIndex]:
    """Capture n distinct (offline) form pages, as `applier inspect` would."""
    snaps = []
    for i in range(n):
        html = FORM.replace("Where did you first hear about this role?", f"Question variant {i}?")
        browser = Browser(tmp / f"b{i}", html)
        recorder = SnapshotRecorder(f"test-{i}", domain)
        with recorder.attach(browser()) as session:
            session.goto(f"https://job-boards.greenhouse.io/acme/jobs/{i}")
            session.page.get_by_role("button", name="Apply").click()
            snaps.append(recorder.capture(session, "inspect"))
    return snaps


def broken() -> FormAdapter:
    spec = builtin_spec("greenhouse")
    spec.fields[0].locator = LocatorSpec(strategy=Strategy.CSS, value="input#firstName")
    spec.version = 99
    return FormAdapter(spec)


def test_good_adapter_passes_and_broken_fails_quickly(fresh_db, tmp_path):
    capture_forms(tmp_path)
    good = run_suite("greenhouse.io", FormAdapter(builtin_spec("greenhouse")))
    bad = run_suite("greenhouse.io", broken())

    assert (good.passed, good.total) == (5, 5)
    assert bad.passed == 0 and all("first_name" in r.failures[0] for r in bad.results)
    assert good.seconds < 30 and bad.seconds < 30


def test_replay_session_cannot_reach_the_live_site(fresh_db, tmp_path):
    [snap] = capture_forms(tmp_path, 1)

    def tries_to_navigate(adapter, session, snapshot):
        session.goto("https://job-boards.greenhouse.io/acme/jobs/1")

    result = replay(snap, FormAdapter(builtin_spec("greenhouse")), [tries_to_navigate])
    assert not result.passed and "replay is offline" in result.failures[0]
    with pytest.raises(RuntimeError):
        ReplaySession(None).goto("https://example.com")  # type: ignore[arg-type]


def test_crashing_assertion_is_a_failure_not_a_crash(fresh_db, tmp_path):
    [snap] = capture_forms(tmp_path, 1)

    def explodes(adapter, session, snapshot):
        raise ValueError("bad check")

    result = replay(snap, FormAdapter(builtin_spec("greenhouse")), [*ATS_FORM_SUITE, explodes])
    assert result.failures == ["explodes raised ValueError: bad check"]


def test_snapshot_selection(fresh_db, tmp_path):
    snaps = capture_forms(tmp_path, 2)
    # Same page captured twice counts once; non-form intents and other domains don't count.
    browser = Browser(tmp_path / "again")
    recorder = SnapshotRecorder("test-again", "job-boards.greenhouse.io")
    with recorder.attach(browser()) as session:
        session.goto("https://job-boards.greenhouse.io/acme/jobs/0")
        recorder.capture(session, "inspect")
        recorder.capture(session, "paused_captcha")
    SnapshotRecorder("other", "jobs.lever.co")  # never captured: no rows
    chosen = form_snapshots("greenhouse.io")
    assert len(chosen) == 2 and {s.url for s in chosen} == {s.url for s in snaps}
    assert form_snapshots("lever.co") == []


def test_suite_cli_is_a_promotion_gate(fresh_db, tmp_path):
    runner = CliRunner()
    assert runner.invoke(sandbox_cli, ["suite", "greenhouse.io"]).exit_code == 1  # no snapshots: not "passing"
    capture_forms(tmp_path, 3)
    ok = runner.invoke(sandbox_cli, ["suite", "greenhouse.io"])
    assert ok.exit_code == 0 and "3/3 passed" in ok.output
    spec_path = tmp_path / "broken.json"
    broken().spec.dump(spec_path)
    failed = runner.invoke(sandbox_cli, ["suite", "greenhouse.io", "--spec", str(spec_path)])
    assert failed.exit_code == 1 and "0/3 passed" in failed.output
