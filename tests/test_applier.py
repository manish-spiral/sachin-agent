"""Phase 7: supervised applier against an offline Greenhouse-like form."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from applier.adapters import builtin_spec, spec_for_url
from applier.adapters.form import FormAdapter
from applier.adapters.spec import LocatorSpec, Strategy
from applier.cli import app as cli
from applier.context import Answer, AnswerSource, ApplicantContext, Unanswerable
from applier.runner import ApplyError, application_url, make_application_id, run_application
from applier.session import BrowserSession
from shared.db.engine import get_session
from shared.db.upsert import upsert_job
from shared.paths import application_dir
from shared.profile.facts import active_facts
from shared.profile.store import save_profile
from shared.schemas.application import Application, ApplicationStatus, PauseReason
from shared.schemas.application_transitions import IllegalApplicationTransition, transition
from shared.schemas.document import DocumentKind, TailoredDocument, make_document_id
from shared.schemas.failure import FailureClass, FailureEvent
from shared.schemas.job import Job, JobStatus, make_job_id
from shared.schemas.profile import Profile

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_form.html"
FORM_URL = "https://job-boards.greenhouse.io/acme/jobs/1"


def _profile(**answers: str) -> Profile:
    return Profile.model_validate({
        "identity": {"first_name": "Arjun", "last_name": "Mehta", "email": "arjun@example.com",
                     "phone": "+91 90000 12345", "linkedin": "linkedin.com/in/arjun"},
        "work_authorization": {"requires_sponsorship": True, "authorized_countries": ["India"]},
        "experience": [{"company": "Razorpay", "title": "Backend Engineer", "bullets": ["Built X"]}],
        "answers": [{"question": "What is your notice period?", "answer": "60 days"},
                    *({"question": q, "answer": a} for q, a in answers.items())],
        "finalized": True,
    })


@pytest.fixture
def applicant(fresh_db, tmp_path) -> Path:
    save_profile(_profile(**{"Where did you first hear about this role?": "LinkedIn"}))
    job = Job(id=make_job_id("greenhouse:acme", "1"), source="greenhouse:acme", external_id="1",
              title="Backend Engineer", company="Acme", url="https://acme.com/careers/1",
              status=JobStatus.APPROVED)
    with get_session() as s:
        upsert_job(s, job)
        for kind in DocumentKind:
            pdf = tmp_path / f"{kind.value}.pdf"
            pdf.write_bytes(b"%PDF-1.4 test\n")
            s.add(TailoredDocument(id=make_document_id(job.id, kind), job_id=job.id, kind=kind,
                                   pdf_path=str(pdf), html_path=str(pdf), verified=True))
        s.commit()
    return tmp_path


JOB_ID = make_job_id("greenhouse:acme", "1")


class Browser:
    """Session factory serving the fixture for the Greenhouse URL, offline."""

    def __init__(self, tmp: Path, html: str | None = None) -> None:
        self.tmp = tmp
        self.html = html or FIXTURE.read_text()
        self.sessions: list[BrowserSession] = []

    def __call__(self) -> BrowserSession:
        browser = self

        class Offline(BrowserSession):
            def start(self) -> BrowserSession:
                super().start()
                assert self.context is not None
                self.context.route("**/*", lambda route: route.fulfill(
                    status=200, content_type="text/html", body=browser.html))
                return self

            def pause_for_human(self, reason: str, *, interactive: bool) -> bool:
                if not interactive:
                    return False
                # The "human" settles the acknowledgement in the browser.
                self.page.locator("#question_5").fill("I acknowledge")
                return True

        session = Offline(self.tmp / "profile", headless=True)
        self.sessions.append(session)
        return session

    def value(self, selector: str) -> str:
        return self.sessions[-1].page.locator(selector).input_value()


def _app() -> Application:
    with get_session() as s:
        row = s.get(Application, make_application_id(JOB_ID))
    assert row is not None
    return row


# --- answers from facts ------------------------------------------------------


def test_answers_come_only_from_facts(fresh_db):
    save_profile(_profile())
    ctx = ApplicantContext(facts=active_facts(include_pii=True))

    sponsor = ctx.answer("Will you require visa sponsorship now or in the future?", ["Yes", "No"])
    assert isinstance(sponsor, Answer) and (sponsor.value, sponsor.source) == ("Yes", AnswerSource.WORK_AUTHORIZATION)
    assert ctx.answer("LinkedIn Profile") == Answer("linkedin.com/in/arjun", AnswerSource.IDENTITY,
                                                    sponsor.fact_id and ctx.answer("LinkedIn Profile").fact_id)  # type: ignore[union-attr]
    notice = ctx.answer("What's your notice period?")
    assert isinstance(notice, Answer) and notice.value == "60 days" and notice.source == AnswerSource.ANSWERS_BANK
    assert ctx.answer("How did you hear about us?") == Unanswerable.UNKNOWN
    assert ctx.answer("I acknowledge the privacy notice") == Unanswerable.CONSENT
    gender = ctx.answer("Gender identity", ["Man", "Woman", "I don't wish to answer"])
    assert isinstance(gender, Answer) and gender.source == AnswerSource.DECLINE
    assert ctx.answer("Are you a veteran?", ["Yes", "No"]) == Unanswerable.SKIP
    # An answer that maps to no dropdown option is not forced onto one.
    assert ctx.answer("What is your notice period?", ["Immediately", "Two weeks"]) == Unanswerable.UNKNOWN


def test_registry_and_application_url():
    assert spec_for_url("https://job-boards.greenhouse.io/x/jobs/1").domain == "greenhouse.io"  # type: ignore[union-attr]
    assert spec_for_url("https://jobs.example.com/1") is None
    job = Job(id="x", source="greenhouse:stripe", external_id="42", title="t", company="c",
              url="https://stripe.com/jobs/search?gh_jid=42")
    assert application_url(job) == "https://job-boards.greenhouse.io/stripe/jobs/42"


def test_application_transitions():
    app = Application(id="a", job_id="j")
    transition(app, ApplicationStatus.PAUSED)
    app.pause_reason = PauseReason.CONSENT
    transition(app, ApplicationStatus.PENDING)
    assert app.pause_reason is None
    transition(app, ApplicationStatus.SUBMITTED)
    with pytest.raises(IllegalApplicationTransition):
        transition(app, ApplicationStatus.PENDING)


# --- end to end against the fixture --------------------------------------------


def test_fills_from_facts_and_pauses_on_consent(applicant):
    browser = Browser(applicant)
    result = run_application(JOB_ID, approve=lambda a, r: pytest.fail("must not ask"), session_factory=browser)

    assert (result.status, result.pause_reason) == (ApplicationStatus.PAUSED, PauseReason.CONSENT)
    assert Path(result.browser_state_path or "").exists()
    steps = [json.loads(line) for line in (application_dir(result.id) / "steps.jsonl").read_text().splitlines()]
    assert [s["step"] for s in steps] == ["open", "filled", "paused_consent"]
    fields = {f["label"]: f for f in steps[1]["fields"]}
    assert fields["Will you require Visa Sponsorship now, or in the future?"]["value"] == "Yes"
    assert fields["What is your notice period?"]["value"] == "60 days"
    assert fields["Where did you first hear about this role?"]["value"] == "LinkedIn"
    assert fields["How would you describe your gender identity?"]["value"] == "I don't wish to answer"
    assert fields["By submitting my application, I acknowledge the Acme privacy notice"]["status"] == "consent"
    assert all(Path(s["screenshot"]).exists() for s in steps)


def test_human_resolves_then_pause_before_submit_then_resume_submits(applicant):
    browser = Browser(applicant)
    seen: dict[str, str] = {}

    def capture_and_decline(_app, _report) -> bool:
        for sel in ("#first_name", "#email", "#question_1", "#question_2", "#question_5"):
            seen[sel] = browser.value(sel)
        seen["resume"] = browser.sessions[-1].page.locator("#resume").evaluate("el => el.files[0].name")
        return False

    paused = run_application(JOB_ID, approve=capture_and_decline, session_factory=browser, interactive=True)
    assert (paused.status, paused.pause_reason) == (ApplicationStatus.PAUSED, PauseReason.SUBMIT_APPROVAL)
    assert seen == {"#first_name": "Arjun", "#email": "arjun@example.com", "#question_1": "Yes",
                    "#question_2": "linkedin.com/in/arjun", "#question_5": "I acknowledge",
                    "resume": "resume.pdf"}

    submitted = run_application(JOB_ID, approve=lambda a, r: True, session_factory=browser, interactive=True)
    assert submitted.status == ApplicationStatus.SUBMITTED and submitted.submitted_at is not None
    assert browser.sessions[-1].context is None  # closed cleanly
    with get_session() as s:
        assert s.get(Job, JOB_ID).status == JobStatus.APPLIED  # type: ignore[union-attr]

    with pytest.raises(ApplyError, match="already submitted"):
        run_application(JOB_ID, approve=lambda a, r: True, session_factory=browser)


def test_refuses_unapproved_job_and_missing_resume(applicant):
    with get_session() as s:
        job = s.get(Job, JOB_ID)
        assert job is not None
        job.status = JobStatus.SCORED
        s.add(job)
        for doc in s.exec(select(TailoredDocument)).all():
            s.delete(doc)
        s.commit()
    with pytest.raises(ApplyError, match="no tailored resume"):
        run_application(JOB_ID, approve=lambda a, r: True, session_factory=Browser(applicant))


def test_selector_drift_fails_and_is_captured(applicant):
    spec = builtin_spec("greenhouse")
    spec.form_marker = LocatorSpec(strategy=Strategy.CSS, value="input#firstName")  # site renamed it
    result = run_application(JOB_ID, approve=lambda a, r: True, session_factory=Browser(applicant),
                             adapter=FormAdapter(spec))

    assert result.status == ApplicationStatus.FAILED
    with get_session() as s:
        failure = s.exec(select(FailureEvent)).one()
    assert (failure.failure_class, failure.application_id) == (FailureClass.SELECTOR_DRIFT, result.id)
    assert "input#firstName" in failure.error_message
    assert (application_dir(result.id) / "page.html").exists()


def test_unknown_site_fails_as_unseen(applicant):
    with get_session() as s:
        job = s.get(Job, JOB_ID)
        assert job is not None
        job.source, job.url = "workable:acme", "https://apply.workable.com/acme/j/1"
        s.add(job)
        s.commit()
    result = run_application(JOB_ID, approve=lambda a, r: True, session_factory=Browser(applicant))
    assert result.status == ApplicationStatus.FAILED
    with get_session() as s:
        assert s.exec(select(FailureEvent)).one().failure_class == FailureClass.UNSEEN_SITE


def test_list_cli(applicant):
    run_application(JOB_ID, approve=lambda a, r: False, session_factory=Browser(applicant))
    out = CliRunner().invoke(cli, ["list"]).output
    assert "paused/consent" in out and "Backend Engineer @ Acme" in out
