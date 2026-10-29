"""Resume content: an LLM plan that cites facts, or a profile-only fallback.

The LLM decides only order and wording of bullets and the summary. Everything
structural — header, employers, titles, dates, education — is rendered from
facts directly, so it cannot be misstated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from matcher.keywords import JobRequirements
from matcher.prompts import RESUME_USER_TEMPLATE, RETRY_FEEDBACK_TEMPLATE
from shared.profile.facts import format_facts_for_prompt
from shared.schemas.job import Job
from shared.schemas.profile import Skills
from shared.schemas.profile_fact import FactKind, ProfileFact

# Facts a resume may state. Answers (notice period, expected pay) and
# preferences belong in application forms, not on a resume.
RESUME_FACT_KINDS = {
    FactKind.IDENTITY,
    FactKind.WORK_AUTHORIZATION,
    FactKind.EXPERIENCE,
    FactKind.EXPERIENCE_BULLET,
    FactKind.TECHNOLOGY,
    FactKind.EDUCATION,
    FactKind.SKILL,
    FactKind.PROJECT,
    FactKind.CERTIFICATION,
}


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    fact_ids: list[str]


class PlannedRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experience_fact_id: str
    bullets: list[Line]


class ResumePlan(BaseModel):
    """What the LLM returns."""

    model_config = ConfigDict(extra="forbid")
    summary: list[Line]
    roles: list[PlannedRole]
    skill_fact_ids: list[str]


@dataclass
class FactBook:
    """The candidate's active facts, indexed for building and checking documents."""

    facts: dict[str, ProfileFact]

    @classmethod
    def of(cls, facts: list[ProfileFact]) -> FactBook:
        return cls({f.id: f for f in facts})

    def kind(self, *kinds: FactKind) -> list[ProfileFact]:
        """Facts of these kinds, in profile order."""
        return sorted((f for f in self.facts.values() if f.kind in kinds), key=lambda f: _path_key(f.path))

    def skills(self) -> list[ProfileFact]:
        """Skill facts in the profile's group order (languages, frameworks,
        tools, ...), not the alphabetical order of their paths."""
        groups = list(Skills.model_fields)
        return sorted(self.kind(FactKind.SKILL), key=lambda f: groups.index(str(f.data["group"])))

    def children(self, parent_id: str, *kinds: FactKind) -> list[ProfileFact]:
        return [f for f in self.kind(*kinds) if f.parent_id == parent_id]

    def for_prompt(self) -> list[ProfileFact]:
        return [f for f in self.facts.values() if not f.pii and f.kind in RESUME_FACT_KINDS]

    def identity(self, name: str) -> str:
        fact = next((f for f in self.kind(FactKind.IDENTITY) if f.path == f"identity.{name}"), None)
        return str(fact.data.get("value", "")) if fact else ""


def _path_key(path: str) -> list[object]:
    """Sort "experience[10]" after "experience[2]"."""
    return [int(p) if p.isdigit() else p for p in re.split(r"\[(\d+)\]", path)]


def requirement_terms(requirements: JobRequirements | None, job: Job) -> list[str]:
    if requirements is None:
        return [w for w in re.findall(r"[A-Za-z][\w+#.]*", job.title) if len(w) > 2]
    return [*requirements.must_have, *requirements.keywords, *requirements.nice_to_have]


def relevance(text: str, terms: list[str]) -> int:
    """How many requirement terms appear in `text` as whole words (crude, free)."""
    lowered = text.lower()
    return sum(
        1 for term in {t.lower() for t in terms if t.strip()}
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lowered)
    )


def build_resume_prompt(
    job: Job, requirements: JobRequirements | None, book: FactBook, feedback: str = ""
) -> str:
    reqs = requirements or JobRequirements(must_have=[], nice_to_have=[], keywords=[], responsibilities=[])
    return RESUME_USER_TEMPLATE.format(
        today=datetime.now(UTC).date().isoformat(),
        title=job.title,
        company=job.company,
        must_have="; ".join(reqs.must_have) or "not stated",
        nice_to_have="; ".join(reqs.nice_to_have) or "not stated",
        keywords=", ".join(reqs.keywords) or "not stated",
        facts=format_facts_for_prompt(book.for_prompt()),
        feedback=feedback,
    )


def retry_feedback(violations: list[str]) -> str:
    return RETRY_FEEDBACK_TEMPLATE.format(violations="\n".join(f"- {v}" for v in violations))



def fallback_plan(book: FactBook, terms: list[str]) -> ResumePlan:
    """Profile-only resume: verbatim bullets, most relevant first. No LLM, no
    rephrasing, so it states exactly the facts and nothing else."""
    roles = []
    for exp in book.kind(FactKind.EXPERIENCE):
        bullets = book.children(exp.id, FactKind.EXPERIENCE_BULLET)
        bullets.sort(key=lambda f: relevance(f.text, terms), reverse=True)
        roles.append(
            PlannedRole(
                experience_fact_id=exp.id,
                bullets=[Line(text=str(b.data["bullet"]), fact_ids=[b.id]) for b in bullets],
            )
        )
    # Stable sort: ties keep the profile's group order.
    skills = sorted(book.skills(), key=lambda f: relevance(str(f.data["skill"]), terms), reverse=True)
    return ResumePlan(summary=[], roles=roles, skill_fact_ids=[f.id for f in skills])


@dataclass
class RenderedRole:
    title: str
    company: str
    dates: str
    bullets: list[str]


@dataclass
class ResumeDocument:
    """Everything the template prints. Built from facts + an accepted plan."""

    name: str
    contact: list[str]
    summary: list[str]
    roles: list[RenderedRole]
    skills: list[str]
    projects: list[str]
    education: list[str]
    certifications: list[str]
    fact_ids: list[str] = field(default_factory=list)

    def expected_lines(self) -> list[str]:
        """Text an ATS must be able to read back out of the PDF, in order."""
        lines = [self.name, *self.summary]
        for role in self.roles:
            lines += [role.title, role.company, *role.bullets]
        return lines + self.education


def build_document(plan: ResumePlan, book: FactBook) -> ResumeDocument:
    contact_fields = ("email", "phone", "linkedin", "github", "portfolio")
    contact = [v for v in (book.identity(n) for n in contact_fields) if v]
    location = book.identity("location")
    if location:
        contact.insert(0, location)

    planned = {r.experience_fact_id: r for r in plan.roles}
    roles: list[RenderedRole] = []
    used: list[str] = [f.id for f in book.kind(FactKind.IDENTITY)]
    for exp in book.kind(FactKind.EXPERIENCE):
        data = exp.data
        start, end = data.get("start_date", ""), data.get("end_date", "")
        role = planned.get(exp.id)
        bullets = [line.text for line in role.bullets] if role else []
        roles.append(
            RenderedRole(
                title=data.get("title", ""),
                company=data.get("company", ""),
                dates=f"{start} – {end or 'Present'}" if start or end else "",
                bullets=bullets,
            )
        )
        used.append(exp.id)
        if role:
            used += [fid for line in role.bullets for fid in line.fact_ids]

    skill_order = [fid for fid in plan.skill_fact_ids if fid in book.facts]
    skill_order += [f.id for f in book.skills() if f.id not in skill_order]
    # A technology fact ("Kafka at Razorpay") may stand in for the skill;
    # print each name once, in the plan's order.
    names = (str(book.facts[fid].data.get("skill") or book.facts[fid].data.get("technology")) for fid in skill_order)
    skills = list(dict.fromkeys(names))

    projects = [f.text.removeprefix("Project ") for f in book.kind(FactKind.PROJECT)]
    education = [f.text for f in book.kind(FactKind.EDUCATION)]
    certifications = [f.text.removeprefix("Certification: ") for f in book.kind(FactKind.CERTIFICATION)]
    used += skill_order + [f.id for f in book.kind(FactKind.PROJECT, FactKind.EDUCATION, FactKind.CERTIFICATION)]
    used += [fid for line in plan.summary for fid in line.fact_ids]

    name = f"{book.identity('first_name')} {book.identity('last_name')}".strip()
    return ResumeDocument(
        name=name,
        contact=contact,
        summary=[line.text for line in plan.summary],
        roles=roles,
        skills=skills,
        projects=projects,
        education=education,
        certifications=certifications,
        fact_ids=list(dict.fromkeys(used)),
    )
