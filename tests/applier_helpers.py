"""Shared offline fixtures for applier tests: a fake job board and a scripted human."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from applier.adapters.base import FillReport
from shared.browser.session import BrowserSession
from shared.db.engine import get_session
from shared.db.upsert import upsert_job
from shared.schemas.application import Application, PauseReason
from shared.schemas.document import DocumentKind, TailoredDocument, make_document_id
from shared.schemas.job import Job, JobStatus, make_job_id
from shared.schemas.profile import Profile

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_form.html"
JOB_ID = make_job_id("greenhouse:acme", "1")


def make_profile(**answers: str) -> Profile:
    return Profile.model_validate({
        "identity": {"first_name": "Arjun", "last_name": "Mehta", "email": "arjun@example.com",
                     "phone": "+91 90000 12345", "linkedin": "linkedin.com/in/arjun",
                     "location": "Bengaluru, India"},
        "work_authorization": {"requires_sponsorship": True, "authorized_countries": ["India"]},
        "experience": [{"company": "Razorpay", "title": "Backend Engineer", "bullets": ["Built X"]}],
        "answers": [{"question": "What is your notice period?", "answer": "60 days"},
                    *({"question": q, "answer": a} for q, a in answers.items())],
        "finalized": True,
    })


def add_job(tmp: Path, external_id: str = "1") -> str:
    job = Job(id=make_job_id("greenhouse:acme", external_id), source="greenhouse:acme",
              external_id=external_id, title="Backend Engineer", company="Acme",
              url=f"https://acme.com/careers/{external_id}", status=JobStatus.APPROVED)
    with get_session() as s:
        upsert_job(s, job)
        for kind in DocumentKind:
            pdf = tmp / f"{kind.value}.pdf"
            pdf.write_bytes(b"%PDF-1.4 test\n")
            s.add(TailoredDocument(id=make_document_id(job.id, kind), job_id=job.id, kind=kind,
                                   pdf_path=str(pdf), html_path=str(pdf), verified=True))
        s.commit()
    return job.id


class Browser:
    """Session factory serving `html` for every page of the job board, offline.
    Sub-resources (e.g. CAPTCHA iframes) get an empty document."""

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

                def serve(route) -> None:
                    main = route.request.url.startswith("https://job-boards.greenhouse.io/")
                    route.fulfill(status=200, content_type="text/html",
                                  body=browser.html if main else "<html></html>")

                self.context.route("**/*", serve)
                return self

        session = Offline(self.tmp / "profile", headless=True)
        self.sessions.append(session)
        return session

    def value(self, selector: str) -> str:
        return self.sessions[-1].page.locator(selector).input_value()


class Scripted:
    """A human whose decisions are scripted; records what it was asked."""

    def __init__(self, *, resolve: Callable[[BrowserSession, PauseReason], bool] | None = None,
                 choose: Callable[[str, str, list[str]], str | None] | None = None,
                 answer: Callable[[str, list[str]], str | None] | None = None,
                 approve: bool | Callable[[Application, FillReport], bool] = False) -> None:
        self._resolve, self._choose, self._answer, self._approve = resolve, choose, answer, approve
        self.asked: list[tuple[str, str]] = []

    def resolve(self, session: BrowserSession, reason: PauseReason, detail: str) -> bool:
        self.asked.append(("resolve", reason.value))
        return bool(self._resolve and self._resolve(session, reason))

    def choose(self, domain: str, prompt: str, options: list[str]) -> str | None:
        self.asked.append(("choose", prompt))
        return self._choose(domain, prompt, options) if self._choose else None

    def answer(self, question: str, options: list[str]) -> str | None:
        self.asked.append(("answer", question))
        return self._answer(question, options) if self._answer else None

    def approve(self, application: Application, report: FillReport) -> bool:
        self.asked.append(("approve", application.id))
        return self._approve(application, report) if callable(self._approve) else self._approve
