"""Tailor a resume and cover letter to one job: draft → verify → retry → fallback.

Every draft is checked by matcher/verifier.py. A draft with any violation is
retried with the violations as feedback, at most MAX_RETRIES times; after that
the profile-only fallback is used. So the output is always verified — the
only question is whether it is tailored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from loguru import logger
from pydantic import BaseModel

from matcher.cover_letter import (
    CoverLetter,
    build_cover_letter_prompt,
    build_letter_document,
    check_cover_letter,
    fallback_letter,
    letter_statements,
)
from matcher.keywords import JobRequirements, get_requirements
from matcher.llm import StructuredLLM
from matcher.pdf import ats_check, html_to_pdf, render_html
from matcher.prompts import (
    COVER_LETTER_PROMPT_VERSION,
    COVER_LETTER_SYSTEM_PROMPT,
    RESUME_PROMPT_VERSION,
    RESUME_SYSTEM_PROMPT,
)
from matcher.resume import (
    FactBook,
    ResumePlan,
    build_document,
    build_resume_prompt,
    fallback_plan,
    requirement_terms,
    retry_feedback,
)
from matcher.verifier import (
    Statement,
    Violation,
    check_resume_plan,
    llm_check,
    plan_statements,
    technology_vocabulary,
)
from shared.db.engine import get_session
from shared.db.failures import domain_from_url, emit_failure
from shared.llm import Task
from shared.paths import cover_letters_dir, resumes_dir
from shared.profile.facts import active_facts
from shared.profile.store import load_current_profile
from shared.schemas.document import DocumentKind, TailoredDocument, make_document_id
from shared.schemas.failure import FailureClass
from shared.schemas.job import Job

MAX_RETRIES = 2
# Retrying cannot help with these; every other failure costs one attempt.
_FATAL = {FailureClass.RATE_LIMIT, FailureClass.AUTH_WALL}

D = TypeVar("D", bound=BaseModel)


class TailorError(RuntimeError):
    pass


@dataclass
class DraftOutcome:
    draft: BaseModel
    attempts: int
    fallback: bool
    rejected: list[Violation] = field(default_factory=list)


@dataclass
class TailorReport:
    job_id: str
    resume: TailoredDocument
    cover_letter: TailoredDocument | None
    ats_problems: list[str]


def _draft_with_verification(
    *,
    job: Job,
    llm: StructuredLLM,
    task: Task,
    system: str,
    prompt: Callable[[str], str],
    model_cls: type[D],
    deterministic: Callable[[D], list[Violation]],
    statements: Callable[[D], list[Statement]],
    fallback: Callable[[], D],
) -> DraftOutcome:
    rejected: list[Violation] = []
    feedback = ""
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            draft = llm(task, system, prompt(feedback), model_cls)
            violations = deterministic(draft)
            if not violations:
                # Only drafts that pass the free checks get the paid one.
                violations = llm_check(statements(draft), llm)
        except Exception as exc:
            failure_class = getattr(exc, "failure_class", FailureClass.UNKNOWN)
            logger.bind(job_id=job.id).error(f"{task.value} attempt {attempt} failed: {exc}")
            emit_failure(
                source_module="matcher",
                phase=f"tailor_{task.value}",
                error=exc,
                failure_class=failure_class,
                domain=domain_from_url(job.url),
                job_id=job.id,
            )
            if failure_class in _FATAL:
                break
            rejected.append(Violation(f"attempt {attempt}", "bad_output", str(exc)[:300]))
            continue
        if not violations:
            return DraftOutcome(draft=draft, attempts=attempt, fallback=False, rejected=rejected)
        logger.bind(job_id=job.id).warning(
            f"{task.value} attempt {attempt} rejected: {len(violations)} violation(s)"
        )
        rejected += violations
        feedback = retry_feedback([str(v) for v in violations])

    draft = fallback()
    leftover = deterministic(draft)
    if leftover:
        # The fallback states verbatim facts; failing here is a bug, not drift.
        raise TailorError(f"profile-only fallback failed verification: {leftover}")
    return DraftOutcome(draft=draft, attempts=MAX_RETRIES + 1, fallback=True, rejected=rejected)


def _requirements_or_none(job: Job, llm: StructuredLLM) -> JobRequirements | None:
    try:
        return get_requirements(job, llm)
    except Exception as exc:
        # Tailoring still works without them; the fallback ranks by title words.
        logger.bind(job_id=job.id).warning(f"requirement extraction failed: {exc}")
        emit_failure(
            source_module="matcher",
            phase="tailor_keywords",
            error=exc,
            failure_class=getattr(exc, "failure_class", FailureClass.UNKNOWN),
            domain=domain_from_url(job.url),
            job_id=job.id,
        )
        return None


def _save_document(document: TailoredDocument) -> TailoredDocument:
    with get_session() as session:
        existing = session.get(TailoredDocument, document.id)
        if existing is not None:
            session.delete(existing)
            session.flush()
        session.add(document)
        session.commit()
        session.refresh(document)
    return document


def tailor_job(job_id: str, *, llm: StructuredLLM, cover_letter: bool = True) -> TailorReport:
    current = load_current_profile()
    if current is None or not current[0].finalized:
        raise TailorError("Tailoring needs a finalized profile (`sachin-agent-profile validate`).")
    profile_version = current[1]
    with get_session() as session:
        job = session.get(Job, job_id)
    if job is None:
        raise TailorError(f"no job {job_id}")

    book = FactBook.of(active_facts(include_pii=True))
    requirements = _requirements_or_none(job, llm)
    terms = requirement_terms(requirements, job)
    vocabulary = technology_vocabulary(book, terms)

    resume_outcome = _draft_with_verification(
        job=job,
        llm=llm,
        task=Task.RESUME,
        system=RESUME_SYSTEM_PROMPT,
        prompt=lambda feedback: build_resume_prompt(job, requirements, book, feedback),
        model_cls=ResumePlan,
        deterministic=lambda plan: check_resume_plan(plan, book, vocabulary),
        statements=lambda plan: plan_statements(plan, book),
        fallback=lambda: fallback_plan(book, terms),
    )
    assert isinstance(resume_outcome.draft, ResumePlan)
    resume_doc = build_document(resume_outcome.draft, book)
    html = render_html("resume.html.j2", doc=resume_doc)
    html_path = resumes_dir() / f"{job.id}.html"
    pdf_path = resumes_dir() / f"{job.id}.pdf"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    html_to_pdf(html, pdf_path)
    ats_problems = ats_check(pdf_path, resume_doc.expected_lines())

    resume = _save_document(
        TailoredDocument(
            id=make_document_id(job.id, DocumentKind.RESUME),
            job_id=job.id,
            kind=DocumentKind.RESUME,
            pdf_path=str(pdf_path),
            html_path=str(html_path),
            fact_ids=resume_doc.fact_ids,
            verified=True,
            violations=[v.as_dict() for v in resume_outcome.rejected],
            fallback=resume_outcome.fallback,
            attempts=resume_outcome.attempts,
            prompt_version=RESUME_PROMPT_VERSION,
            model=None if resume_outcome.fallback else llm.model_name(Task.RESUME),
            profile_version=profile_version,
        )
    )

    letter: TailoredDocument | None = None
    if cover_letter:
        letter_outcome = _draft_with_verification(
            job=job,
            llm=llm,
            task=Task.COVER_LETTER,
            system=COVER_LETTER_SYSTEM_PROMPT,
            prompt=lambda feedback: build_cover_letter_prompt(job, requirements, book, feedback),
            model_cls=CoverLetter,
            deterministic=lambda draft: check_cover_letter(draft, job, book, vocabulary),
            statements=lambda draft: letter_statements(draft, job, book),
            fallback=lambda: fallback_letter(job, book, terms),
        )
        assert isinstance(letter_outcome.draft, CoverLetter)
        letter_doc = build_letter_document(letter_outcome.draft, job, book)
        letter_html = render_html("cover_letter.html.j2", doc=letter_doc)
        letter_html_path = cover_letters_dir() / f"{job.id}.html"
        letter_pdf_path = cover_letters_dir() / f"{job.id}.pdf"
        letter_html_path.parent.mkdir(parents=True, exist_ok=True)
        letter_html_path.write_text(letter_html, encoding="utf-8")
        html_to_pdf(letter_html, letter_pdf_path)
        ats_problems += [f"cover letter: {p}" for p in ats_check(letter_pdf_path, letter_doc.expected_lines())]
        letter = _save_document(
            TailoredDocument(
                id=make_document_id(job.id, DocumentKind.COVER_LETTER),
                job_id=job.id,
                kind=DocumentKind.COVER_LETTER,
                pdf_path=str(letter_pdf_path),
                html_path=str(letter_html_path),
                fact_ids=letter_doc.fact_ids,
                verified=True,
                violations=[v.as_dict() for v in letter_outcome.rejected],
                fallback=letter_outcome.fallback,
                attempts=letter_outcome.attempts,
                prompt_version=COVER_LETTER_PROMPT_VERSION,
                model=None if letter_outcome.fallback else llm.model_name(Task.COVER_LETTER),
                profile_version=profile_version,
            )
        )

    return TailorReport(job_id=job.id, resume=resume, cover_letter=letter, ats_problems=ats_problems)
