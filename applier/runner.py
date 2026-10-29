"""Run one application end to end: open → fill → pause → (human approves) → submit.

Default is pause-before-submit: nothing is submitted unless `approve` returns
True for this exact filled form. Every step is logged with a screenshot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlmodel import select

from applier.adapters import adapter_for_url
from applier.adapters.base import FillReport
from applier.adapters.form import FormAdapter, SelectorNotFound
from applier.context import ApplicantContext
from applier.session import BrowserSession
from shared.config import get_settings
from shared.db.engine import get_session
from shared.db.failures import domain_from_url, emit_failure
from shared.paths import application_dir
from shared.profile.facts import active_facts
from shared.schemas.application import Application, ApplicationStatus, PauseReason
from shared.schemas.application_transitions import transition as app_transition
from shared.schemas.document import DocumentKind, TailoredDocument
from shared.schemas.failure import FailureClass
from shared.schemas.job import Job, JobStatus
from shared.schemas.job_transitions import transition as job_transition

SessionFactory = Callable[[], BrowserSession]
# Decides, for one filled form, whether to submit. The CLI asks the human.
Approver = Callable[[Application, FillReport], bool]


class ApplyError(RuntimeError):
    pass


def make_application_id(job_id: str) -> str:
    """One application per job: re-running resumes it instead of applying twice."""
    return hashlib.sha256(f"application|{job_id}".encode()).hexdigest()


def application_url(job: Job) -> str:
    """Greenhouse postings often link to a company careers page; the form
    itself lives on the job board under the same board slug and id."""
    family, _, board = job.source.partition(":")
    if family == "greenhouse" and board:
        return f"https://job-boards.greenhouse.io/{board}/jobs/{job.external_id}"
    return job.url


def default_session() -> BrowserSession:
    return BrowserSession(Path(get_settings().browser_profile_dir), headless=True)


@dataclass
class StepLog:
    application: Application
    directory: Path

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._n = len(list(self.directory.glob("*.png")))

    def record(self, step: str, session: BrowserSession | None, **detail: object) -> None:
        self._n += 1
        shot = None
        if session is not None:
            try:
                shot = str(session.screenshot(self.directory / f"{self._n:02d}-{step}.png"))
            except Exception as exc:  # a dead page must not hide the step itself
                detail["screenshot_error"] = str(exc)
        entry = {"ts": datetime.now(UTC).isoformat(), "step": step, "screenshot": shot, **detail}
        with (self.directory / "steps.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        self.application.last_step = step
        _save(self.application)
        logger.bind(application_id=self.application.id).info(f"step {step} {detail or ''}")


def _save(app: Application) -> Application:
    app.updated_at = datetime.now(UTC)
    with get_session() as s:
        merged = s.merge(app)
        s.commit()
        s.refresh(merged)
    return merged


def _document(job_id: str, kind: DocumentKind) -> TailoredDocument | None:
    with get_session() as s:
        return s.exec(
            select(TailoredDocument).where(TailoredDocument.job_id == job_id, TailoredDocument.kind == kind)
        ).first()


def load_application(application_id: str) -> Application:
    with get_session() as s:
        app = s.get(Application, application_id)
    if app is None:
        raise ApplyError(f"no application {application_id}")
    return app


def _prepare(job_id: str) -> tuple[Job, Application, ApplicantContext]:
    with get_session() as s:
        job = s.get(Job, job_id)
    if job is None:
        raise ApplyError(f"no job {job_id}")
    resume = _document(job_id, DocumentKind.RESUME)
    if resume is None:
        raise ApplyError("no tailored resume; run `sachin-agent-matcher tailor <job_id>` first")
    letter = _document(job_id, DocumentKind.COVER_LETTER)

    with get_session() as s:
        app = s.get(Application, make_application_id(job_id))
    if app is None:
        if job.status != JobStatus.APPROVED:
            raise ApplyError(f"job is {job.status.value}; approve it with `sachin-agent-matcher approve`")
        app = Application(id=make_application_id(job_id), job_id=job_id)
    if app.status in {ApplicationStatus.SUBMITTED, ApplicationStatus.ABANDONED}:
        raise ApplyError(f"application already {app.status.value}")
    app_transition(app, ApplicationStatus.PENDING)
    app.resume_path = resume.pdf_path
    app.cover_letter_path = letter.pdf_path if letter else None
    app = _save(app)

    applicant = ApplicantContext(
        facts=active_facts(include_pii=True),
        resume_path=Path(resume.pdf_path),
        cover_letter_path=Path(letter.pdf_path) if letter else None,
    )
    return job, app, applicant


def _pause(app: Application, reason: PauseReason, session: BrowserSession, log: StepLog) -> Application:
    state = session.save_state(log.directory / "storage_state.json")
    session.dom(log.directory / "page.html")
    app.browser_state_path = str(state)
    app_transition(app, ApplicationStatus.PAUSED)
    app.pause_reason = reason
    log.record(f"paused_{reason.value}", session)
    return _save(app)


def _fail(app: Application, job: Job, log: StepLog, session: BrowserSession | None,
          exc: Exception, failure_class: FailureClass, phase: str) -> Application:
    log.record(f"failed_{phase}", session, error=str(exc))
    emit_failure(
        source_module="applier",
        phase=phase,
        error=exc,
        failure_class=failure_class,
        domain=domain_from_url(application_url(job)),
        job_id=job.id,
        application_id=app.id,
        context={"last_step": app.last_step},
    )
    app_transition(app, ApplicationStatus.FAILED)
    return _save(app)


def run_application(
    job_id: str,
    *,
    approve: Approver,
    session_factory: SessionFactory = default_session,
    interactive: bool = False,
    adapter: FormAdapter | None = None,
) -> Application:
    job, app, applicant = _prepare(job_id)
    log = StepLog(app, application_dir(app.id))
    url = application_url(job)
    adapter = adapter or adapter_for_url(url)
    if adapter is None:
        return _fail(app, job, log, None, ApplyError(f"no adapter for {url}"),
                     FailureClass.UNSEEN_SITE, "select_adapter")
    app.adapter_version_id = f"{adapter.spec.domain}@v{adapter.spec.version}"

    with session_factory() as session:
        if app.browser_state_path:
            session.restore_state(Path(app.browser_state_path))
        try:
            adapter.init(session, url)
            log.record("open", session, url=session.page.url)
            if not adapter.detect(session):
                raise SelectorNotFound("form", adapter.spec.form_marker)
            report = adapter.fill(session, applicant)
        except SelectorNotFound as exc:
            session.dom(log.directory / "page.html")
            return _fail(app, job, log, session, exc, FailureClass.SELECTOR_DRIFT, "fill")
        except Exception as exc:
            return _fail(app, job, log, session, exc, FailureClass.UNKNOWN, "fill")

        log.record("filled", session, fields=[f.__dict__ for f in report.fields])
        if report.needs_human:
            first = report.needs_human[0]
            reason = PauseReason.CONSENT if first.status == "consent" else PauseReason.UNKNOWN_FIELD
            labels = [f.label for f in report.needs_human]
            if not session.pause_for_human(f"Needs you: {labels}", interactive=interactive):
                return _pause(app, reason, session, log)
            log.record("human_completed_fields", session, fields=labels)

        if not approve(app, report):
            return _pause(app, PauseReason.SUBMIT_APPROVAL, session, log)

        log.record("approved", session)
        try:
            confirmed = adapter.submit(session)
        except Exception as exc:
            return _fail(app, job, log, session, exc, FailureClass.UNKNOWN, "submit")
        if not confirmed:
            return _fail(app, job, log, session, ApplyError("no confirmation after submit"),
                         FailureClass.SEMANTIC_MISMATCH, "submit")

        log.record("submitted", session)
        app_transition(app, ApplicationStatus.SUBMITTED)
        app.submitted_at = datetime.now(UTC)
        app = _save(app)

    with get_session() as s:
        row = s.get(Job, job.id)
        if row is not None:
            job_transition(row, JobStatus.APPLIED)
            s.add(row)
            s.commit()
    return app
