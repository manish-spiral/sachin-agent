"""Phase 10: every step and API call leaves an offline-replayable snapshot."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from sqlmodel import col, select
from typer.testing import CliRunner

from applier.adapters import builtin_spec
from applier.adapters.form import FormAdapter
from applier.adapters.spec import LocatorSpec, Strategy
from applier.runner import run_application
from sandbox.cli import app as sandbox_cli
from sandbox.offline import load_snapshot, offline_page
from scraper.runner import run_all_sources
from shared.db.engine import get_session
from shared.schemas.failure import FailureEvent
from shared.schemas.snapshot import SnapshotIndex
from shared.signature import cosine, html_features, json_features, page_signature
from tests.applier_helpers import FIXTURE, JOB_ID, Browser, Scripted

FORM = FIXTURE.read_text()


def _snapshots(**where) -> list[SnapshotIndex]:
    query = select(SnapshotIndex).order_by(col(SnapshotIndex.step_index))
    for key, value in where.items():
        query = query.where(getattr(SnapshotIndex, key) == value)
    with get_session() as s:
        return list(s.exec(query).all())


# --- signatures ------------------------------------------------------------------


def test_signature_ignores_per_job_questions_but_not_structure():
    other_job = FORM.replace("question_6", "question_9").replace("gender identity", "pronouns")
    redesign = FORM.replace('id="first_name"', 'id="firstName"')
    base = html_features(FORM)
    assert page_signature(base) == page_signature(html_features(other_job))
    assert page_signature(base) != page_signature(html_features(redesign))
    assert cosine(base, html_features(redesign)) > 0.9  # still very similar: same ATS
    login = html_features("<form><input type=password><button>Sign in</button></form>")
    assert cosine(base, login) < 0.7


def test_json_signature_tracks_schema_not_values():
    a = json_features({"jobs": [{"id": 1, "title": "x"}]})
    b = json_features({"jobs": [{"id": 2, "title": "y"}, {"id": 3, "title": "z"}]})
    drifted = json_features({"jobs": [{"id": 1, "name": "x"}]})
    assert page_signature(a) == page_signature(b)
    assert page_signature(a) != page_signature(drifted)


# --- browser capture ---------------------------------------------------------------


def test_every_step_is_snapshotted_with_har_and_trace(applicant):
    result = run_application(JOB_ID, human=Scripted(choose=lambda d, p, o: o[0]), session_factory=Browser(applicant))

    snaps = _snapshots()
    assert [s.intent for s in snaps][:2] == ["open", "filled"] and snaps[-1].intent == "paused_submit_approval"
    assert {s.domain for s in snaps} == {"job-boards.greenhouse.io"}
    assert len({s.run_id for s in snaps}) == 1 and snaps[0].run_id.startswith(result.id[:12])
    for snap in snaps:
        for path in (snap.html_path, snap.a11y_path, snap.screenshot_path):
            assert path and Path(path).exists()
        assert Path(snap.html_path).with_name("meta.json").exists()
        assert snap.page_signature and snap.page_signature.startswith("sig1:") and snap.features
    # Run-level artifacts are flushed when the browser closes.
    assert Path(snaps[0].har_path or "").exists() and Path(snaps[0].trace_path or "").exists()
    assert "textbox" in Path(snaps[1].a11y_path or "").read_text()
    # Same form before and after filling: same structure, same signature.
    assert snaps[0].page_signature == snaps[1].page_signature


def test_failure_points_at_its_snapshot(applicant):
    spec = builtin_spec("greenhouse")
    spec.fields[0].locator = LocatorSpec(strategy=Strategy.CSS, value="input#firstName")
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant), adapter=FormAdapter(spec))

    with get_session() as s:
        failure = s.exec(select(FailureEvent)).one()
    assert failure.snapshot_id is not None
    snap = load_snapshot(failure.snapshot_id)
    assert 'id="first_name"' in Path(snap.html_path).read_text()  # the evidence a healer needs


# --- offline replay -------------------------------------------------------------------


def test_replay_is_offline_and_faithful(applicant):
    run_application(JOB_ID, human=Scripted(choose=lambda d, p, o: o[0]), session_factory=Browser(applicant))
    filled = next(s for s in _snapshots() if s.intent == "filled")

    with offline_page(filled) as page:
        # Values typed during the run are in the captured DOM...
        assert page.title() == "Job Application for Backend Engineer at Acme"
        assert page.locator("#first_name").count() == 1
        # ...and nothing can reach the network.
        response = page.evaluate("fetch('https://example.com/').then(() => 'reached', () => 'blocked')")
        assert response == "blocked"

    with offline_page(filled, javascript=True) as page:
        page.locator("#question_4").click()
        assert page.get_by_role("option").count() == 3  # inline scripts still work


def test_replay_cli_and_list(applicant):
    run_application(JOB_ID, human=Scripted(choose=lambda d, p, o: o[0]), session_factory=Browser(applicant))
    snap = _snapshots()[0]
    runner = CliRunner()
    assert snap.id in runner.invoke(sandbox_cli, ["list"]).output
    out = runner.invoke(sandbox_cli, ["replay", snap.id, "--screenshot", str(applicant / "r.png")]).output
    assert "(offline" in out and "Submit application" in out and (applicant / "r.png").exists()
    assert runner.invoke(sandbox_cli, ["replay", "nope"]).exit_code == 1


# --- API capture -----------------------------------------------------------------------


def test_api_responses_are_snapshotted_with_schema_signature(fresh_db):
    payload = {"jobs": [{"id": 1, "title": "Engineer", "location": {"name": "Remote"}}]}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    asyncio.run(run_all_sources(["greenhouse"], companies_override=["acme"], transport=transport))

    [snap] = _snapshots(intent="api:fetch")
    saved = json.loads(Path(snap.html_path).read_text())
    assert saved["jobs"][0]["title"] == "Engineer" and snap.domain == "greenhouse.io"
    assert "key:jobs[].location.name" in snap.features

    drifted = {"jobs": [{"id": 1, "name": "Engineer"}]}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=drifted))
    asyncio.run(run_all_sources(["greenhouse"], companies_override=["acme"], transport=transport))
    signatures = {s.page_signature for s in _snapshots(intent="api:fetch")}
    assert len(signatures) == 2  # the schema change is visible as a new signature

    out = CliRunner().invoke(sandbox_cli, ["replay", snap.id]).output
    assert "1 jobs fetched" in out


def test_prune(applicant):
    run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant))
    run_dir = Path(_snapshots()[0].html_path).parents[1]
    assert CliRunner().invoke(sandbox_cli, ["prune", "--days", "30"]).output.startswith("pruned 0")
    out = CliRunner().invoke(sandbox_cli, ["prune", "--days", "-1"]).output
    assert out.startswith("pruned") and not run_dir.exists() and _snapshots() == []
