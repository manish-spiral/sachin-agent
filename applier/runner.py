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
from sqlmodel import col, select

from applier.adapters import adapter_for_url
from applier.adapters.base import FillReport
from applier.adapters.form import FormAdapter, SelectorNotFound
from applier.context import Answer, ApplicantContext
from applier.human import Absent, Human
from applier.memory import consents_for, remember_answer, remember_consent
from shared.browser.detectors import Blocker, click_consent, detect_blocker
from shared.browser.session import BrowserSession
from shared.browser.snapshot import SnapshotRecorder, new_run_id
from shared.config import get_settings
from shared.db.engine import get_session
from shared.db.failures import domain_from_url, emit_failure
from shared.killswitch import is_killed
from shared.notify import notify
from shared.paths import application_dir
from shared.profile.facts import active_facts
from shared.schemas.application import Application, ApplicationStatus, PauseReason
from shared.schemas.application_transitions import transition as app_transition
from shared.schemas.consent import ConsentKind
from shared.schemas.document import DocumentKind, TailoredDocument
from shared.schemas.failure import FailureClass
from shared.schemas.job import Job, JobStatus
from shared.schemas.job_transitions import transition as job_transition
from shared.schemas.profile_fact import ProfileFact

SessionFactory = Callable[[], BrowserSession]
InferrerFactory = Callable[[list[ProfileFact]], Callable[[str, list[str] | None], Answer | None]]
# A page can stack walls (consent banner, then login): bounded so a page that
# re-shows a banner forever cannot loop.
MAX_BLOCKER_ROUNDS = 4


class ApplyError(RuntimeError):
    pass


class Escalation(RuntimeError):
    """The run needed a human: recorded as a failure event, not raised."""


# Pauses that mean "could not do it alone". SUBMIT_APPROVAL is by design and
# KILL_SWITCH is the human's own action; neither is an escalation.
_ESCALATIONS = {
    PauseReason.CAPTCHA: FailureClass.CAPTCHA,
    PauseReason.LOGIN: FailureClass.AUTH_WALL,
    PauseReason.CONSENT: FailureClass.CONSENT,
    PauseReason.UNKNOWN_FIELD: FailureClass.UNKNOWN_FIELD,
}


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
    recorder: SnapshotRecorder | None = None

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._n = len(list(self.directory.glob("*.png")))

    @property
    def snapshot_id(self) -> str | None:
        return self.recorder.last.id if self.recorder and self.recorder.last else None

    def record(self, step: str, session: BrowserSession | None, **detail: object) -> None:
        self._n += 1
        shot = None
        if session is not None:
            try:
                if self.recorder is not None:
                    snap = self.recorder.capture(session, step)
                    shot, detail["snapshot_id"] = snap.screenshot_path, snap.id
                else:
                    shot = str(session.screenshot(self.directory / f"{self._n:02d}-{step}.png"))
            except Exception as exc:  # a dead page must not hide the step itself
                detail["capture_error"] = str(exc)
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


def _prepare(job_id: str) -> tuple[Job, Application]:
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

    return job, app


def _pause(app: Application, job: Job, reason: PauseReason, session: BrowserSession,
           log: StepLog, detail: str = "") -> Application:
    """Persist everything needed to resume — browser state, screenshot, DOM —
    then tell the human."""
    state = session.save_state(log.directory / "storage_state.json")
    session.dom(log.directory / "page.html")
    app.browser_state_path = str(state)
    app_transition(app, ApplicationStatus.PAUSED)
    app.pause_reason = reason
    log.record(f"paused_{reason.value}", session, detail=detail)
    if reason in _ESCALATIONS:
        # The agent could not continue on its own: count it (Phase 16's
        # escalation rate) and keep the evidence (Phase 11 diagnosis).
        emit_failure(
            source_module="applier",
            phase=f"pause_{reason.value}",
            error=Escalation(detail or reason.value),
            failure_class=_ESCALATIONS[reason],
            domain=domain_from_url(application_url(job)),
            job_id=job.id,
            application_id=app.id,
            snapshot_id=log.snapshot_id,
        )
    shot = log.recorder.last.screenshot_path if log.recorder and log.recorder.last else None
    shots = sorted(log.directory.glob("*.png"))
    photo = Path(shot) if shot else (shots[-1] if shots else None)
    notify(
        f"⏸ {job.title} @ {job.company}: paused ({reason.value}). {detail[:300]}\n"
        f"Resume: sachin-agent-applier resume {app.id[:12]}",
        photo=photo,
    )
    return _save(app)


def _fail(app: Application, job: Job, log: StepLog, session: BrowserSession | None,
          exc: Exception, failure_class: FailureClass, phase: str) -> Application:
    context: dict[str, object] = {"last_step": app.last_step}
    if isinstance(exc, SelectorNotFound):
        # What the healer needs: which intent broke and the locator that failed.
        context |= {"intent": exc.intent, "adapter_version": app.adapter_version_id,
                    "locator": exc.locator.model_dump(exclude_none=True)}
    log.record(f"failed_{phase}", session, error=str(exc))
    emit_failure(
        source_module="applier",
        phase=phase,
        error=exc,
        failure_class=failure_class,
        domain=domain_from_url(application_url(job)),
        job_id=job.id,
        application_id=app.id,
        snapshot_id=log.snapshot_id,
        context=context,
    )
    app_transition(app, ApplicationStatus.FAILED)
    return _save(app)


def _applicant(job_id: str, domain: str, inferrer: InferrerFactory | None) -> ApplicantContext:
    resume = _document(job_id, DocumentKind.RESUME)
    letter = _document(job_id, DocumentKind.COVER_LETTER)
    facts = active_facts(include_pii=True)
    return ApplicantContext(
        facts=facts,
        resume_path=Path(resume.pdf_path) if resume else None,
        cover_letter_path=Path(letter.pdf_path) if letter else None,
        consents=[(c.prompt, c.choice) for c in consents_for(domain, ConsentKind.ACKNOWLEDGEMENT)],
        inferrer=inferrer(facts) if inferrer else None,
    )


def _clear_blockers(session: BrowserSession, domain: str, human: Human, log: StepLog) -> Blocker | None:
    """Replay remembered banner decisions; hand CAPTCHAs, logins and new
    consents to the human. Returns the blocker still standing, if any."""
    for _ in range(MAX_BLOCKER_ROUNDS):
        blocker = detect_blocker(session.page)
        if blocker is None:
            return None
        if blocker.reason == PauseReason.CONSENT:
            remembered = next((c.choice for c in consents_for(domain, ConsentKind.BANNER)
                               if c.choice in blocker.options), None)
            choice = remembered or human.choose(domain, blocker.detail, list(blocker.options))
            if choice is None:
                return blocker
            if remembered is None:
                remember_consent(domain, ConsentKind.BANNER, blocker.detail, choice)
            click_consent(session.page, choice)
            log.record("consent_banner", session, choice=choice, remembered=remembered is not None)
            continue
        if not human.resolve(session, blocker.reason, blocker.detail):
            return blocker
        log.record(f"human_resolved_{blocker.reason.value}", session)
    return detect_blocker(session.page)


def _ask_human(report: FillReport, domain: str, human: Human, log: StepLog) -> bool:
    """Ask for every unknown / consent field. True if all got an answer.
    Answers are taught to the answers bank, consents to consent memory."""
    complete = True
    for f in report.needs_human:
        if f.status == "consent":
            choice = human.choose(domain, f.label, f.options)
            if choice is None:
                complete = False
                continue
            remember_consent(domain, ConsentKind.ACKNOWLEDGEMENT, f.label, choice)
        else:
            value = human.answer(f.label, f.options)
            if value is None:
                complete = False
                continue
            remember_answer(f.label, value)
        log.record("human_answered", None, question=f.label)
    return complete


def applications_today() -> int:
    """Applications started since UTC midnight: what the daily cap counts."""
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as s:
        return len(s.exec(select(Application).where(Application.created_at >= midnight)).all())


def approved_ready(limit: int) -> list[Job]:
    """Approved jobs with a resume and no finished application, best first."""
    finished = select(Application.job_id).where(
        col(Application.status).in_([ApplicationStatus.SUBMITTED, ApplicationStatus.ABANDONED])
    )
    has_resume = select(TailoredDocument.job_id).where(TailoredDocument.kind == DocumentKind.RESUME)
    with get_session() as s:
        return list(s.exec(
            select(Job)
            .where(Job.status == JobStatus.APPROVED, col(Job.id).in_(has_resume), col(Job.id).not_in(finished))
            .order_by(col(Job.score).desc())
            .limit(limit)
        ).all())


def run_application(
    job_id: str,
    *,
    human: Human | None = None,
    session_factory: SessionFactory = default_session,
    adapter: FormAdapter | None = None,
    inferrer: InferrerFactory | None = None,
) -> Application:
    human = human or Absent()
    if is_killed():
        raise ApplyError("kill switch is on")
    job, app = _prepare(job_id)
    recorder = SnapshotRecorder(new_run_id(app.id[:12]), domain_from_url(application_url(job)) or "") \
        if get_settings().capture_snapshots else None
    log = StepLog(app, application_dir(app.id), recorder)
    url = application_url(job)
    domain = domain_from_url(url) or ""
    adapter = adapter or adapter_for_url(url)
    if adapter is None:
        return _fail(app, job, log, None, ApplyError(f"no adapter for {url}"),
                     FailureClass.UNSEEN_SITE, "select_adapter")
    app.adapter_version_id = f"{adapter.spec.domain}@v{adapter.spec.version}"

    session = session_factory()
    if recorder is not None:
        recorder.attach(session)
    with session:
        if app.browser_state_path:
            session.restore_state(Path(app.browser_state_path))
        try:
            adapter.init(session, url)
            log.record("open", session, url=session.page.url)
            blocker = _clear_blockers(session, domain, human, log)
            if blocker is not None:
                return _pause(app, job, blocker.reason, session, log, blocker.detail)
            if not adapter.detect(session):
                raise SelectorNotFound("form", adapter.spec.form_marker)
            report = adapter.fill(session, _applicant(job.id, domain, inferrer))
            log.record("filled", session, fields=[f.__dict__ for f in report.fields])
            if report.needs_human:
                if not _ask_human(report, domain, human, log):
                    first = report.needs_human[0]
                    reason = PauseReason.CONSENT if first.status == "consent" else PauseReason.UNKNOWN_FIELD
                    return _pause(app, job, reason, session, log,
                                  "; ".join(f.label for f in report.needs_human))
                # Re-fill: the newly taught answers and consents now resolve.
                report = adapter.fill(session, _applicant(job.id, domain, inferrer))
                log.record("refilled", session, fields=[f.__dict__ for f in report.fields])
                if report.needs_human:
                    return _pause(app, job, PauseReason.UNKNOWN_FIELD, session, log,
                                  "; ".join(f.label for f in report.needs_human))
        except SelectorNotFound as exc:
            session.dom(log.directory / "page.html")
            return _fail(app, job, log, session, exc, FailureClass.SELECTOR_DRIFT, "fill")
        except Exception as exc:
            return _fail(app, job, log, session, exc, FailureClass.UNKNOWN, "fill")

        if is_killed():
            return _pause(app, job, PauseReason.KILL_SWITCH, session, log, "kill switch thrown mid-run")
        if not human.approve(app, report):
            return _pause(app, job, PauseReason.SUBMIT_APPROVAL, session, log, "ready to submit")

        log.record("approved", session)
        try:
            confirmed = adapter.submit(session)
            if not confirmed:
                # A challenge often appears only on submit.
                blocker = _clear_blockers(session, domain, human, log)
                if blocker is not None:
                    return _pause(app, job, blocker.reason, session, log, blocker.detail)
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
