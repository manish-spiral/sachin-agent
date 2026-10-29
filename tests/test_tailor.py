"""Phase 6: resume tailoring with verification, fallback, PDF and ATS check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlmodel import select
from typer.testing import CliRunner

from matcher.cli import app
from matcher.cover_letter import (
    CoverLetter,
    Paragraph,
    Sentence,
    check_cover_letter,
    fallback_letter,
)
from matcher.keywords import JobRequirements
from matcher.llm import LLMError
from matcher.pdf import ats_check, html_to_pdf, render_html
from matcher.resume import FactBook, Line, PlannedRole, ResumePlan, fallback_plan
from matcher.tailor import MAX_RETRIES, tailor_job
from matcher.verifier import (
    Statement,
    Verdict,
    Verdicts,
    check_resume_plan,
    llm_check,
    technology_vocabulary,
)
from shared.db.engine import get_session
from shared.db.upsert import upsert_job
from shared.llm import Task
from shared.profile.facts import active_facts
from shared.profile.store import save_profile
from shared.schemas.document import DocumentKind, TailoredDocument
from shared.schemas.failure import FailureClass, FailureEvent
from shared.schemas.job import Job, make_job_id
from shared.schemas.profile import Profile
from shared.schemas.profile_fact import FactKind

REQUIREMENTS = JobRequirements(
    must_have=["5+ years backend", "Go"],
    nice_to_have=["Rust"],
    keywords=["Go", "PostgreSQL", "Kafka", "Rust", "payments"],
    responsibilities=["build payment services"],
)


def _profile() -> Profile:
    return Profile.model_validate({
        "identity": {"first_name": "Arjun", "last_name": "Mehta", "email": "arjun@example.com",
                     "phone": "+91 90000 12345", "location": "Bengaluru, India"},
        "experience": [
            {"company": "Razorpay", "title": "Senior Backend Engineer", "start_date": "Apr 2022",
             "end_date": "Present",
             "bullets": ["Designed the payouts reconciliation service in Go, processing 3M transactions/day",
                         "Cut p99 latency of the settlement API from 800ms to 120ms"],
             "technologies": ["Go", "PostgreSQL", "Kafka"]},
            {"company": "Delhivery", "title": "Backend Engineer", "start_date": "Jul 2019",
             "end_date": "Mar 2022",
             "bullets": ["Built the shipment-tracking API in Python (Django) serving 25k requests/minute"],
             "technologies": ["Python", "Django"]},
        ],
        "education": [{"institution": "PES University", "degree": "B.E.", "field": "Computer Science"}],
        "skills": {"languages": ["Go", "Python"], "tools": ["PostgreSQL", "Kafka"]},
        "finalized": True,
    })


@pytest.fixture
def book(fresh_db) -> FactBook:
    save_profile(_profile())
    return FactBook.of(active_facts(include_pii=True))


def _fact(book: FactBook, kind: FactKind, contains: str) -> str:
    return next(f.id for f in book.kind(kind) if contains in f.text)


def _good_plan(book: FactBook) -> ResumePlan:
    razorpay = _fact(book, FactKind.EXPERIENCE, "Razorpay")
    delhivery = _fact(book, FactKind.EXPERIENCE, "Delhivery")
    payouts = _fact(book, FactKind.EXPERIENCE_BULLET, "payouts")
    latency = _fact(book, FactKind.EXPERIENCE_BULLET, "p99")
    tracking = _fact(book, FactKind.EXPERIENCE_BULLET, "shipment")
    return ResumePlan(
        summary=[Line(text="Backend engineer building payment systems in Go at Razorpay.",
                      fact_ids=[razorpay, payouts])],
        roles=[
            PlannedRole(experience_fact_id=razorpay, bullets=[
                Line(text="Built a Go payouts reconciliation service handling 3M transactions/day",
                     fact_ids=[payouts]),
                Line(text="Reduced settlement API p99 latency from 800ms to 120ms", fact_ids=[latency]),
            ]),
            PlannedRole(experience_fact_id=delhivery, bullets=[
                Line(text="Built a Django shipment-tracking API in Python at 25k requests/minute",
                     fact_ids=[tracking]),
            ]),
        ],
        skill_fact_ids=[f.id for f in book.kind(FactKind.SKILL)],
    )


def _vocab(book: FactBook) -> set[str]:
    return technology_vocabulary(book, [*REQUIREMENTS.keywords, *REQUIREMENTS.must_have])


def _rules(violations) -> list[str]:
    return [v.rule for v in violations]


# --- deterministic verifier ---------------------------------------------


def test_faithful_rephrase_passes(book):
    assert check_resume_plan(_good_plan(book), book, _vocab(book)) == []


def test_invented_number_is_caught(book):
    plan = _good_plan(book)
    plan.roles[0].bullets[1].text = "Reduced settlement API p99 latency by 90%"
    assert _rules(check_resume_plan(plan, book, _vocab(book))) == ["invented_number"]


def test_skill_copied_from_job_is_caught(book):
    plan = _good_plan(book)
    plan.roles[0].bullets[0].text = "Built a Go and Rust payouts service handling 3M transactions/day"
    violations = check_resume_plan(plan, book, _vocab(book))
    assert _rules(violations) == ["invented_term"]
    assert "Rust" in violations[0].detail


def test_short_terms_are_case_sensitive(book):
    plan = _good_plan(book)
    # "go" the verb is not the language Go; "go-live" must not be flagged.
    plan.roles[1].bullets[0].text = (
        "Took the Django shipment-tracking API in Python to go-live at 25k requests/minute"
    )
    assert check_resume_plan(plan, book, _vocab(book)) == []


def test_achievement_moved_to_another_employer_is_caught(book):
    plan = _good_plan(book)
    payouts = plan.roles[0].bullets[0]
    plan.roles[1].bullets.append(payouts)
    assert "misattributed" in _rules(check_resume_plan(plan, book, _vocab(book)))


def test_dropped_role_unknown_ids_and_pii_are_caught(book):
    plan = _good_plan(book)
    plan.roles.pop()
    email = next(f.id for f in book.kind(FactKind.IDENTITY) if f.path == "identity.email")
    plan.summary[0].fact_ids.append(email)
    plan.skill_fact_ids.append("F0000000000")
    plan.roles[0].bullets.append(Line(text="Led a team", fact_ids=["F1234567890"]))
    rules = _rules(check_resume_plan(plan, book, _vocab(book)))
    assert {"missing_role", "pii_fact", "unknown_skill", "unknown_fact", "no_achievement"} <= set(rules)


def test_uncited_line_is_caught(book):
    plan = _good_plan(book)
    plan.summary.append(Line(text="Passionate team player.", fact_ids=[]))
    assert _rules(check_resume_plan(plan, book, _vocab(book))) == ["uncited"]


def test_fallback_plan_always_verifies(book):
    plan = fallback_plan(book, REQUIREMENTS.keywords)
    assert check_resume_plan(plan, book, _vocab(book)) == []
    # Most relevant verbatim bullet first: payouts (Go, payments...) before latency.
    assert "payouts" in plan.roles[0].bullets[0].text
    assert plan.summary == []
    names = [book.facts[f].data["skill"] for f in plan.skill_fact_ids]
    # Relevant first (Go, PostgreSQL, Kafka are job keywords); ties keep group order.
    assert names == ["Go", "PostgreSQL", "Kafka", "Python"]


# --- LLM verifier ------------------------------------------------------------


class FakeLLM:
    """Scripted responses per output model; records every call."""

    def __init__(self, **scripts: Any) -> None:
        self.scripts = {k: list(v) if isinstance(v, list) else v for k, v in scripts.items()}
        self.calls: list[tuple[Task, str]] = []

    def model_name(self, task: Task) -> str:
        return f"fake:{task.value}"

    def __call__(self, task: Task, system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        self.calls.append((task, user))
        script = self.scripts[model_cls.__name__]
        item = script.pop(0) if isinstance(script, list) else script
        if isinstance(item, Exception):
            raise item
        return item(user) if callable(item) else item


def _all_supported(user: str) -> Verdicts:
    count = sum(1 for line in user.splitlines() if ". statement: " in line)
    return Verdicts(verdicts=[Verdict(index=i, supported=True, unsupported="") for i in range(1, count + 1)])


def test_llm_check_flags_unsupported_and_missing_verdicts():
    statements = [Statement("a", "x", ["src"]), Statement("b", "y", ["src"]), Statement("c", "z", [])]
    llm = FakeLLM(Verdicts=Verdicts(verdicts=[
        Verdict(index=1, supported=True, unsupported=""),
        Verdict(index=2, supported=False, unsupported="led a team of 10"),
    ]))
    violations = llm_check(statements, llm)  # type: ignore[arg-type]
    assert [(v.where, v.rule) for v in violations] == [("b", "unsupported"), ("c", "unverified")]


# --- end to end ----------------------------------------------------------


@pytest.fixture
def job(book) -> Job:
    job = Job(
        id=make_job_id("greenhouse:stripe", "1"),
        source="greenhouse:stripe",
        external_id="1",
        title="Software Engineer, Payments",
        company="stripe",
        location_raw="Bengaluru",
        description_raw="We build payments infrastructure in Go and PostgreSQL. 5+ years required.",
        url="https://boards.greenhouse.io/stripe/jobs/1",
    )
    with get_session() as s:
        upsert_job(s, job)
    return job


def _letter(book: FactBook) -> CoverLetter:
    payouts = _fact(book, FactKind.EXPERIENCE_BULLET, "payouts")
    return CoverLetter(paragraphs=[
        Paragraph(sentences=[Sentence(text="Stripe builds payments infrastructure in Go and PostgreSQL.",
                                      about="company", fact_ids=[])]),
        Paragraph(sentences=[Sentence(text="At Razorpay I designed a Go payouts reconciliation "
                                           "service processing 3M transactions/day.",
                                      about="candidate", fact_ids=[payouts])]),
    ])


def _documents() -> dict[DocumentKind, TailoredDocument]:
    with get_session() as s:
        return {d.kind: d for d in s.exec(select(TailoredDocument)).all()}


def test_tailor_accepts_verified_draft_and_writes_ats_safe_pdf(book, job):
    llm = FakeLLM(JobRequirements=REQUIREMENTS, ResumePlan=_good_plan(book),
                  CoverLetter=_letter(book), Verdicts=_all_supported)

    report = tailor_job(job.id, llm=llm)  # type: ignore[arg-type]

    assert report.ats_problems == []
    docs = _documents()
    resume = docs[DocumentKind.RESUME]
    assert (resume.verified, resume.fallback, resume.attempts) == (True, False, 1)
    assert resume.model == "fake:resume"
    assert Path(resume.pdf_path).exists()
    from pypdf import PdfReader

    text = " ".join(p.extract_text() for p in PdfReader(resume.pdf_path).pages)
    assert "Reduced settlement API p99 latency from 800ms to 120ms" in text
    assert "arjun@example.com" in text  # contact comes from facts, never from the LLM
    assert _fact(book, FactKind.EXPERIENCE_BULLET, "p99") in resume.fact_ids
    assert docs[DocumentKind.COVER_LETTER].fallback is False


def test_tailor_retries_with_feedback_then_accepts(book, job):
    bad = _good_plan(book)
    bad.roles[0].bullets[1].text = "Reduced settlement API p99 latency by 90%"
    llm = FakeLLM(JobRequirements=REQUIREMENTS, ResumePlan=[bad, _good_plan(book)],
                  Verdicts=_all_supported)

    tailor_job(job.id, llm=llm, cover_letter=False)  # type: ignore[arg-type]

    resume = _documents()[DocumentKind.RESUME]
    assert (resume.attempts, resume.fallback) == (2, False)
    assert [v["rule"] for v in resume.violations] == ["invented_number"]
    resume_prompts = [user for task, user in llm.calls if task == Task.RESUME]
    assert "previous_draft_rejected" in resume_prompts[1] and "90" in resume_prompts[1]


def test_tailor_falls_back_after_max_retries(book, job):
    bad = _good_plan(book)
    bad.roles[0].bullets[0].text = "Built a Rust payouts service handling 3M transactions/day"
    llm = FakeLLM(JobRequirements=REQUIREMENTS, ResumePlan=bad, Verdicts=_all_supported)

    tailor_job(job.id, llm=llm, cover_letter=False)  # type: ignore[arg-type]

    resume = _documents()[DocumentKind.RESUME]
    assert resume.fallback is True and resume.verified is True
    assert len([t for t, _ in llm.calls if t == Task.RESUME]) == MAX_RETRIES + 1
    assert resume.model is None
    from pypdf import PdfReader

    text = " ".join(p.extract_text() for p in PdfReader(resume.pdf_path).pages)
    assert "Rust" not in text
    assert "Designed the payouts reconciliation service in Go" in text  # verbatim


def test_llm_check_rejection_counts_as_a_failed_attempt(book, job):
    unsupported = Verdicts(verdicts=[Verdict(index=1, supported=False, unsupported="payment systems")])
    llm = FakeLLM(JobRequirements=REQUIREMENTS, ResumePlan=_good_plan(book),
                  Verdicts=[unsupported, _all_supported])

    tailor_job(job.id, llm=llm, cover_letter=False)  # type: ignore[arg-type]

    resume = _documents()[DocumentKind.RESUME]
    assert resume.attempts == 2
    assert resume.violations[0]["rule"] == "unsupported"


def test_llm_outage_falls_back_and_records_failure(book, job):
    outage = LLMError("tokens per day", failure_class=FailureClass.RATE_LIMIT)
    llm = FakeLLM(JobRequirements=outage, ResumePlan=outage, CoverLetter=outage)

    report = tailor_job(job.id, llm=llm)  # type: ignore[arg-type]

    assert report.resume.fallback and report.cover_letter and report.cover_letter.fallback
    assert report.ats_problems == []
    with get_session() as s:
        phases = sorted(f.phase for f in s.exec(select(FailureEvent)).all())
    assert phases == ["tailor_cover_letter", "tailor_keywords", "tailor_resume"]


def test_requirements_are_cached(book, job):
    llm = FakeLLM(JobRequirements=[REQUIREMENTS], ResumePlan=_good_plan(book), Verdicts=_all_supported)
    tailor_job(job.id, llm=llm, cover_letter=False)  # type: ignore[arg-type]
    tailor_job(job.id, llm=llm, cover_letter=False)  # type: ignore[arg-type]
    assert [t for t, _ in llm.calls].count(Task.KEYWORDS) == 1
    assert len(_documents()) == 1  # re-tailoring replaces, never duplicates


# --- cover letter ------------------------------------------------------------


def test_cover_letter_checks(book, job):
    vocab = _vocab(book)
    assert check_cover_letter(_letter(book), job, book, vocab) == []

    lying = _letter(book)
    lying.paragraphs[0].sentences[0].text = "Stripe processes 10 billion payments in Rust."
    lying.paragraphs[1].sentences[0].fact_ids = []
    rules = _rules(check_cover_letter(lying, job, book, vocab))
    assert rules == ["invented_number", "invented_term", "uncited"]


def test_fallback_letter_verifies(book, job):
    letter = fallback_letter(job, book, REQUIREMENTS.keywords)
    assert check_cover_letter(letter, job, book, _vocab(book)) == []


# --- rendering ---------------------------------------------------------------


def test_template_is_single_column_without_tables_or_images(book):
    html = render_html("resume.html.j2", doc=__import__("matcher.resume", fromlist=["x"]).build_document(
        _good_plan(book), book))
    for banned in ("<table", "<img", "<svg", "column-count", "display: grid", "float:"):
        assert banned not in html


def test_ats_check_reports_missing_and_out_of_order(tmp_path):
    pdf = tmp_path / "x.pdf"
    html_to_pdf("<h1>Alpha</h1><p>Beta</p>", pdf)
    assert ats_check(pdf, ["Alpha", "Beta"]) == []
    problems = ats_check(pdf, ["Beta", "Alpha", "Gamma"])
    assert any("out of order" in p for p in problems)
    assert any("missing" in p and "Gamma" in p for p in problems)


def test_tailor_cli_needs_exactly_one_target(fresh_db):
    result = CliRunner().invoke(app, ["tailor"])
    assert result.exit_code == 2


def test_technology_fact_in_skills_is_accepted_and_deduplicated(book):
    from matcher.resume import build_document

    plan = _good_plan(book)
    kafka_tech = _fact(book, FactKind.TECHNOLOGY, "Kafka")
    plan.skill_fact_ids = [kafka_tech, *plan.skill_fact_ids]
    assert check_resume_plan(plan, book, _vocab(book)) == []
    skills = build_document(plan, book).skills
    assert skills[0] == "Kafka" and skills.count("Kafka") == 1


def test_technology_only_bullet_is_rejected(book):
    plan = _good_plan(book)
    kafka_tech = _fact(book, FactKind.TECHNOLOGY, "Kafka")
    plan.roles[0].bullets.append(Line(text="Built event pipelines with Kafka", fact_ids=[kafka_tech]))
    assert _rules(check_resume_plan(plan, book, _vocab(book))) == ["no_achievement"]


def test_malformed_output_is_retried_but_quota_is_not(book, job):
    malformed = LLMError("json_validate_failed", failure_class=FailureClass.SEMANTIC_MISMATCH)
    llm = FakeLLM(JobRequirements=REQUIREMENTS, ResumePlan=[malformed, _good_plan(book)],
                  Verdicts=_all_supported)

    tailor_job(job.id, llm=llm, cover_letter=False)  # type: ignore[arg-type]

    resume = _documents()[DocumentKind.RESUME]
    assert (resume.attempts, resume.fallback) == (2, False)
    assert resume.violations[0]["rule"] == "bad_output"


def test_number_formats_are_compared_by_value(book):
    from matcher.verifier import numbers_in

    assert numbers_in("25k") == numbers_in("25,000") == numbers_in("25 thousand")
    assert numbers_in("3M/day") == numbers_in("3 million per day")
    assert numbers_in("800ms") == {"800"}

    plan = _good_plan(book)
    # Seen live: "25,000" for the fact's "25k" was wrongly rejected.
    plan.roles[1].bullets[0].text = "Built a Django shipment-tracking API in Python at 25,000 requests/minute"
    assert check_resume_plan(plan, book, _vocab(book)) == []
    # Seen live: "improved latency by 850 ms" — arithmetic the facts don't state.
    plan.roles[0].bullets[1].text = "Improved settlement API p99 latency by 850ms"
    assert _rules(check_resume_plan(plan, book, _vocab(book))) == ["invented_number"]
