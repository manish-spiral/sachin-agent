"""Cover letter: an LLM draft whose every sentence is either cited to facts or
grounded in the job text, plus a profile-only fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from matcher.keywords import JobRequirements
from matcher.prompts import COVER_LETTER_USER_TEMPLATE
from matcher.resume import FactBook, Line, relevance
from matcher.verifier import Statement, Violation, check_line, mentions, numbers_in
from shared.profile.facts import format_facts_for_prompt
from shared.schemas.job import Job
from shared.schemas.profile_fact import FactKind

JOB_TEXT_CHARS = 3000


class Sentence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    about: Literal["candidate", "company"]
    fact_ids: list[str]


class Paragraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentences: list[Sentence]


class CoverLetter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraphs: list[Paragraph]


def build_cover_letter_prompt(
    job: Job, requirements: JobRequirements | None, book: FactBook, feedback: str = ""
) -> str:
    return COVER_LETTER_USER_TEMPLATE.format(
        title=job.title,
        company=job.company,
        must_have="; ".join(requirements.must_have) if requirements else "not stated",
        description=job.description_raw[:JOB_TEXT_CHARS],
        facts=format_facts_for_prompt(book.for_prompt()),
        feedback=feedback,
    )


def check_cover_letter(
    letter: CoverLetter, job: Job, book: FactBook, vocabulary: set[str]
) -> list[Violation]:
    if not letter.paragraphs:
        return [Violation("letter", "empty", "has no paragraphs")]
    out: list[Violation] = []
    job_text = f"{job.title} {job.company} {job.description_raw}"
    for p, paragraph in enumerate(letter.paragraphs):
        for s, sentence in enumerate(paragraph.sentences):
            where = f"paragraph {p + 1} sentence {s + 1}"
            if sentence.about == "candidate":
                line = Line(text=sentence.text, fact_ids=sentence.fact_ids)
                out += check_line(where, line, book, vocabulary=vocabulary)
                continue
            # A company sentence's only source is the job text.
            if sentence.fact_ids:
                out.append(Violation(where, "company_cites_facts",
                                     "a company sentence must not cite candidate facts"))
            invented = numbers_in(sentence.text) - numbers_in(job_text)
            if invented:
                out.append(Violation(where, "invented_number",
                                     f"{', '.join(sorted(invented))} not in the job text: {sentence.text!r}"))
            terms = sorted(t for t in vocabulary if mentions(sentence.text, t) and not mentions(job_text, t))
            if terms:
                out.append(Violation(where, "invented_term",
                                     f"names {', '.join(terms)}, which the job text doesn't: {sentence.text!r}"))
    return out


def letter_statements(letter: CoverLetter, job: Job, book: FactBook) -> list[Statement]:
    out = []
    excerpt = f"Job posting ({job.title} at {job.company}): {job.description_raw[:JOB_TEXT_CHARS]}"
    for p, paragraph in enumerate(letter.paragraphs):
        for s, sentence in enumerate(paragraph.sentences):
            where = f"paragraph {p + 1} sentence {s + 1}"
            if sentence.about == "candidate":
                sources = [book.facts[f].text for f in sentence.fact_ids if f in book.facts]
            else:
                sources = [excerpt]
            out.append(Statement(where, sentence.text, sources))
    return out


def fallback_letter(job: Job, book: FactBook, terms: list[str]) -> CoverLetter:
    """Template letter from verbatim facts. Plain, but every word is true."""
    opening = Sentence(text=f"I am applying for the {job.title} role at {job.company}.",
                       about="company", fact_ids=[])
    paragraphs = [Paragraph(sentences=[opening])]

    experiences = book.kind(FactKind.EXPERIENCE)
    if experiences:
        latest = experiences[0]
        body = [Sentence(text=f"My most recent role: {latest.text}.", about="candidate", fact_ids=[latest.id])]
        bullets = [b for e in experiences for b in book.children(e.id, FactKind.EXPERIENCE_BULLET)]
        bullets.sort(key=lambda b: relevance(b.text, terms), reverse=True)
        body += [Sentence(text=f"{b.text}.", about="candidate", fact_ids=[b.id]) for b in bullets[:3]]
        paragraphs.append(Paragraph(sentences=body))

    skills = sorted(book.kind(FactKind.SKILL), key=lambda f: relevance(str(f.data["skill"]), terms), reverse=True)[:6]
    if skills:
        names = ", ".join(str(f.data["skill"]) for f in skills)
        paragraphs.append(Paragraph(sentences=[
            Sentence(text=f"My skills include {names}.", about="candidate", fact_ids=[f.id for f in skills])
        ]))
    return CoverLetter(paragraphs=paragraphs)


@dataclass
class CoverLetterDocument:
    name: str
    contact: list[str]
    company: str
    title: str
    paragraphs: list[str]
    fact_ids: list[str]

    def expected_lines(self) -> list[str]:
        return [self.name, *self.paragraphs]


def build_letter_document(letter: CoverLetter, job: Job, book: FactBook) -> CoverLetterDocument:
    contact = [v for v in (book.identity(n) for n in ("location", "email", "phone")) if v]
    fact_ids = [f for p in letter.paragraphs for s in p.sentences for f in s.fact_ids]
    return CoverLetterDocument(
        name=f"{book.identity('first_name')} {book.identity('last_name')}".strip(),
        contact=contact,
        company=job.company,
        title=job.title,
        paragraphs=[" ".join(s.text for s in p.sentences) for p in letter.paragraphs],
        fact_ids=list(dict.fromkeys(fact_ids)),
    )
