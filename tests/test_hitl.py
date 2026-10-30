"""Phase 8: detect walls, pause reliably, notify, remember the human's decisions."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from applier.answering import FactInferrer, InferredAnswer
from applier.context import Answer, AnswerSource, ApplicantContext
from applier.memory import consents_for
from applier.runner import make_application_id, run_application
from shared.browser.detectors import detect_captcha, detect_consent, detect_login_wall
from shared.config import reset_settings
from shared.db.engine import get_session
from shared.notify import notify
from shared.paths import application_dir
from shared.profile.facts import active_facts
from shared.profile.store import load_profile
from shared.schemas.application import Application, ApplicationStatus, PauseReason
from shared.schemas.consent import ConsentKind
from shared.schemas.profile_fact import FactKind
from tests.applier_helpers import FIXTURE, JOB_ID, Browser, Scripted, add_job

FORM = FIXTURE.read_text()
BANNER = """<div id="onetrust-banner-sdk" role="dialog">We use cookies to improve your experience.
<button onclick="this.parentElement.remove()">Accept all</button>
<button onclick="this.parentElement.remove()">Reject all</button></div>"""
CAPTCHA = """<iframe id="cap" src="https://www.google.com/recaptcha/api2/bframe?k=x"
 style="width:300px;height:200px"></iframe>"""
INVISIBLE_CAPTCHA = """<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"
 style="width:256px;height:60px"></iframe>"""


def _with(snippet: str) -> str:
    return FORM.replace("<body>", "<body>" + snippet, 1)


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, Path | None]]:
    calls: list[tuple[str, Path | None]] = []
    monkeypatch.setattr("applier.runner.notify", lambda text, photo=None: calls.append((text, photo)) or True)
    return calls


def _app() -> Application:
    with get_session() as s:
        app = s.get(Application, make_application_id(JOB_ID))
    assert app is not None
    return app


def _page(browser: Browser, html: str):
    session = browser().start()
    session.context.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=html))  # type: ignore[union-attr]
    session.goto("https://job-boards.greenhouse.io/acme/jobs/1")
    return session


# --- detectors -------------------------------------------------------------------


def test_detectors(tmp_path):
    browser = Browser(tmp_path)
    session = _page(browser, _with(CAPTCHA))
    try:
        assert detect_captcha(session.page).reason == PauseReason.CAPTCHA  # type: ignore[union-attr]
        session.page.set_content(_with(INVISIBLE_CAPTCHA))
        assert detect_captcha(session.page) is None  # invisible reCAPTCHA is not a wall
        session.page.set_content("<p>Please verify you are human to continue.</p>")
        assert detect_captcha(session.page) is not None
        session.page.set_content("<form><input type='email'><input type='password'></form>")
        assert detect_login_wall(session.page).reason == PauseReason.LOGIN  # type: ignore[union-attr]
        session.page.set_content(_with(BANNER))
        consent = detect_consent(session.page)
        assert consent is not None and consent.options == ("Accept all", "Reject all")
        session.page.set_content(FORM)
        assert (detect_captcha(session.page), detect_login_wall(session.page), detect_consent(session.page)) == (None, None, None)
    finally:
        session.close()


# --- pausing and resuming --------------------------------------------------------


def test_captcha_pauses_with_state_screenshot_dom_and_notification(applicant, sent):
    result = run_application(JOB_ID, human=Scripted(), session_factory=Browser(applicant, _with(CAPTCHA)))

    assert (result.status, result.pause_reason) == (ApplicationStatus.PAUSED, PauseReason.CAPTCHA)
    directory = application_dir(result.id)
    assert (directory / "storage_state.json").exists() and (directory / "page.html").exists()
    (text, photo), = sent
    assert "captcha" in text and f"resume {result.id[:12]}" in text
    assert photo is not None and photo.exists()


def test_human_solves_captcha_then_run_continues(applicant, sent):
    def solve(session, reason):
        session.page.evaluate("document.getElementById('cap').remove()")
        return True

    human = Scripted(resolve=solve, choose=lambda d, p, o: o[0])
    result = run_application(JOB_ID, human=human, session_factory=Browser(applicant, _with(CAPTCHA)))

    assert (result.status, result.pause_reason) == (ApplicationStatus.PAUSED, PauseReason.SUBMIT_APPROVAL)
    assert ("resolve", "captcha") in human.asked
    steps = [json.loads(x)["step"] for x in (application_dir(result.id) / "steps.jsonl").read_text().splitlines()]
    assert "human_resolved_captcha" in steps


def test_consent_banner_decided_once_then_replayed(applicant, sent):
    browser = Browser(applicant, _with(BANNER))
    paused = run_application(JOB_ID, human=Scripted(), session_factory=browser)
    assert paused.pause_reason == PauseReason.CONSENT and len(sent) == 1  # never auto-accepted

    decides = Scripted(choose=lambda d, prompt, options: "Reject all" if "cookies" in prompt else options[0])
    run_application(JOB_ID, human=decides, session_factory=browser)
    [banner] = consents_for("job-boards.greenhouse.io", ConsentKind.BANNER)
    assert banner.choice == "Reject all"

    # A later application on the same domain: no question about the banner.
    third = Scripted(approve=False)
    add_job(applicant, external_id="2")
    from shared.schemas.job import make_job_id

    run_application(make_job_id("greenhouse:acme", "2"), human=third, session_factory=browser)
    assert not any(kind == "choose" for kind, _ in third.asked)


def test_unknown_answer_is_taught_and_reused(applicant, sent):
    from shared.profile.store import save_profile
    from tests.applier_helpers import make_profile

    save_profile(make_profile())  # no answer for "Where did you first hear…"
    first = Scripted(answer=lambda q, o: "Referral", choose=lambda d, p, o: o[0])
    run_application(JOB_ID, human=first, session_factory=Browser(applicant))

    assert ("answer", "Where did you first hear about this role?") in first.asked
    profile = load_profile()
    assert profile is not None and any(a.answer == "Referral" for a in profile.answers)
    assert any(f.kind == FactKind.ANSWER and "Referral" in f.text for f in active_facts())

    add_job(applicant, external_id="3")
    from shared.schemas.job import make_job_id

    second = Scripted()
    run_application(make_job_id("greenhouse:acme", "3"), human=second, session_factory=Browser(applicant))
    assert [kind for kind, _ in second.asked] == ["approve"]  # nothing left to ask


def test_unanswered_question_pauses_and_notifies(applicant, sent):
    from shared.profile.store import save_profile
    from tests.applier_helpers import make_profile

    save_profile(make_profile())
    result = run_application(JOB_ID, human=Scripted(choose=lambda d, p, o: o[0]), session_factory=Browser(applicant))
    assert result.pause_reason == PauseReason.UNKNOWN_FIELD
    assert "Where did you first hear about this role?" in sent[-1][0]


# --- LLM inference gate ------------------------------------------------------------


class _LLM:
    def __init__(self, result: InferredAnswer) -> None:
        self.result = result

    def __call__(self, task, system, user, model_cls):
        return self.result


def test_inference_needs_confidence_citations_and_a_valid_option(applicant):
    facts = active_facts(include_pii=True)
    location = next(f.id for f in facts if f.path == "identity.location")
    q, options = "Are you currently based in the United States?", ["Yes", "No"]

    def infer(answer: str, ids: list[str], confidence: float):
        return FactInferrer(_LLM(InferredAnswer(answer=answer, fact_ids=ids, confidence=confidence)), facts)(q, options)  # type: ignore[arg-type]

    assert infer("No", [location], 0.95) == Answer("No", AnswerSource.INFERRED, location)
    assert infer("No", [location], 0.5) is None  # not confident enough
    assert infer("No", ["F0000000000"], 0.95) is None  # cites a fact that doesn't exist
    assert infer("Nope", [location], 0.95) is None  # not one of the options
    assert infer("No", [], 0.95) is None  # no citation
    email = next(f.id for f in facts if f.path == "identity.email")
    assert infer("No", [email], 0.95) is None  # PII facts are never shown, so never citable

    ctx = ApplicantContext(facts=facts, inferrer=lambda question, opts: None)
    assert ctx.answer(q, options).value == "unknown"  # type: ignore[union-attr]


# --- Telegram ------------------------------------------------------------------------


def test_notify(monkeypatch, tmp_path, caplog):
    assert notify("hi") is False  # unconfigured: log only

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    reset_settings()
    requests: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG")
    assert notify("paused", photo=shot, transport=httpx.MockTransport(ok)) is True
    assert requests[0].url.path.endswith("/sendPhoto")
    assert notify("paused", transport=httpx.MockTransport(ok)) is True
    assert requests[1].url.path.endswith("/sendMessage")

    from loguru import logger

    messages: list[str] = []
    sink = logger.add(messages.append)
    assert notify("x", transport=httpx.MockTransport(lambda r: httpx.Response(500))) is False
    logger.remove(sink)
    assert messages and not any("SECRET" in m for m in messages)
    reset_settings()


def test_consent_cli_roundtrip(fresh_db):
    from typer.testing import CliRunner

    from applier.cli import app as cli

    runner = CliRunner()
    runner.invoke(cli, ["consent", "set", "jobs.example.com", "I acknowledge the notice", "I acknowledge"])
    assert "I acknowledge" in runner.invoke(cli, ["consent", "list"]).output
    assert "Forgot 1" in runner.invoke(cli, ["consent", "forget", "jobs.example.com"]).output
    with get_session() as s:
        from shared.schemas.consent import ConsentChoice

        assert s.exec(select(ConsentChoice)).all() == []
