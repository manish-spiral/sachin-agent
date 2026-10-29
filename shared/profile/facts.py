"""Explode a Profile into citable facts, keep the table in sync, and check
citations. See shared/schemas/profile_fact.py for why facts exist."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from shared.db.engine import get_session
from shared.schemas.profile import Profile
from shared.schemas.profile_fact import FactKind, ProfileFact

FACT_ID_PREFIX = "F"
_ID_RE = re.compile(rf"^{FACT_ID_PREFIX}[0-9a-f]{{10}}$")


def make_fact_id(kind: FactKind, natural_key: str) -> str:
    """Stable across saves: depends on what the fact is, not where it sits."""
    digest = hashlib.sha256(f"{kind.value}|{natural_key.strip().lower()}".encode()).hexdigest()
    return f"{FACT_ID_PREFIX}{digest[:10]}"


@dataclass
class _Draft:
    kind: FactKind
    key: str
    text: str
    path: str
    parent_key: tuple[FactKind, str] | None = None
    data: dict[str, Any] = field(default_factory=dict)
    pii: bool = False

    @property
    def id(self) -> str:
        return make_fact_id(self.kind, self.key)


def _dates(start: str, end: str) -> str:
    if not start and not end:
        return ""
    return f"{start or '?'} – {end or 'Present'}"


def _drafts(p: Profile) -> list[_Draft]:
    out: list[_Draft] = []
    ident = p.identity
    for name in ("first_name", "last_name", "email", "phone", "linkedin", "github", "portfolio"):
        value = getattr(ident, name)
        if value:
            out.append(_Draft(FactKind.IDENTITY, name, f"{name}: {value}", f"identity.{name}",
                              data={"value": str(value)}, pii=True))
    if ident.location:
        out.append(_Draft(FactKind.IDENTITY, "location", f"Based in {ident.location}", "identity.location",
                          data={"value": ident.location}))

    auth = p.work_authorization
    if auth.requires_sponsorship is not None:
        # Scope matters: a bare "Requires visa sponsorship" made the scorer
        # reject a job in the candidate's own country (seen in calibration).
        need = "Requires" if auth.requires_sponsorship else "Does not require"
        scope = f" to work outside {', '.join(auth.authorized_countries)}" if auth.authorized_countries else ""
        out.append(_Draft(FactKind.WORK_AUTHORIZATION, "requires_sponsorship",
                          f"{need} visa sponsorship{scope}", "work_authorization.requires_sponsorship"))
    for i, country in enumerate(auth.authorized_countries):
        out.append(_Draft(FactKind.WORK_AUTHORIZATION, f"country|{country}",
                          f"Authorized to work in {country}", f"work_authorization.authorized_countries[{i}]",
                          data={"country": country}))
    if auth.visa_type:
        out.append(_Draft(FactKind.WORK_AUTHORIZATION, "visa_type",
                          f"Visa type: {auth.visa_type}", "work_authorization.visa_type"))

    prefs = p.preferences
    for name, label in (("target_titles", "Target role"), ("seniority", "Target seniority"),
                        ("preferred_locations", "Preferred location"),
                        ("excluded_companies", "Will not work at")):
        for i, value in enumerate(getattr(prefs, name)):
            out.append(_Draft(FactKind.PREFERENCE, f"{name}|{value}", f"{label}: {value}",
                              f"preferences.{name}[{i}]"))
    out.append(_Draft(FactKind.PREFERENCE, "remote_ok",
                      "Open to remote work" if prefs.remote_ok else "Not open to remote work",
                      "preferences.remote_ok"))
    if prefs.salary_floor is not None:
        out.append(_Draft(FactKind.PREFERENCE, "salary_floor",
                          f"Minimum acceptable salary: {prefs.salary_floor}", "preferences.salary_floor"))

    for i, exp in enumerate(p.experience):
        exp_key = f"{exp.company}|{exp.title}|{exp.start_date}"
        dates = _dates(exp.start_date, exp.end_date)
        out.append(_Draft(FactKind.EXPERIENCE, exp_key,
                          f"{exp.title} at {exp.company}" + (f", {dates}" if dates else ""),
                          f"experience[{i}]",
                          data={"company": exp.company, "title": exp.title,
                                "start_date": exp.start_date, "end_date": exp.end_date}))
        parent = (FactKind.EXPERIENCE, exp_key)
        for j, bullet in enumerate(exp.bullets):
            out.append(_Draft(FactKind.EXPERIENCE_BULLET, f"{exp_key}|{bullet}",
                              f"At {exp.company}: {bullet}", f"experience[{i}].bullets[{j}]",
                              parent_key=parent, data={"bullet": bullet}))
        for j, tech in enumerate(exp.technologies):
            out.append(_Draft(FactKind.TECHNOLOGY, f"{exp_key}|{tech}",
                              f"Used {tech} at {exp.company}", f"experience[{i}].technologies[{j}]",
                              parent_key=parent, data={"technology": tech}))

    for i, edu in enumerate(p.education):
        degree = " ".join(x for x in (edu.degree, edu.field) if x)
        dates = _dates(edu.start_date, edu.end_date)
        out.append(_Draft(FactKind.EDUCATION, f"{edu.institution}|{edu.degree}|{edu.field}",
                          ", ".join(x for x in (degree, edu.institution) if x) + (f" ({dates})" if dates else ""),
                          f"education[{i}]", data=edu.model_dump()))

    for group, values in p.skills.model_dump().items():
        for i, value in enumerate(values):
            out.append(_Draft(FactKind.SKILL, f"{group}|{value}", f"Skill ({group}): {value}",
                              f"skills.{group}[{i}]", data={"skill": value, "group": group}))

    for i, proj in enumerate(p.projects):
        text = f"Project {proj.name}" + (f": {proj.description}" if proj.description else "")
        if proj.technologies:
            text += f" (technologies: {', '.join(proj.technologies)})"
        out.append(_Draft(FactKind.PROJECT, proj.name, text, f"projects[{i}]", data=proj.model_dump()))

    for i, cert in enumerate(p.certifications):
        text = cert.name + (f" from {cert.issuer}" if cert.issuer else "") + (f", {cert.date}" if cert.date else "")
        out.append(_Draft(FactKind.CERTIFICATION, f"{cert.name}|{cert.issuer}", f"Certification: {text}",
                          f"certifications[{i}]", data=cert.model_dump()))

    for i, qa in enumerate(p.answers):
        out.append(_Draft(FactKind.ANSWER, qa.question, f"Q: {qa.question} A: {qa.answer}",
                          f"answers[{i}]", data=qa.model_dump()))
    return out


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


def _evidence_index(draft: _Draft, folded_lines: list[str], start: int = 0) -> int | None:
    """Index of the first CV line (from `start`) containing the fact's value as
    whole words. Children search from their parent's line, so "Used Go at X"
    cites a line from X's section rather than the first "Go" in the CV."""
    needles = [v for v in draft.data.values() if isinstance(v, str) and len(v) >= 2] or [draft.text]
    for needle in needles:
        folded = _fold(needle)
        if not folded:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(folded)}(?!\w)")
        for i in range(start, len(folded_lines)):
            if pattern.search(folded_lines[i]):
                return i
    return None


@dataclass(frozen=True)
class SyncReport:
    added: int
    updated: int
    deactivated: int
    active: int


def sync_facts(
    session: Session, profile: Profile, *, version: int, source_text: str | None = None
) -> SyncReport:
    """Upsert the facts of `profile`; deactivate (never delete) facts it no longer has."""
    drafts = {d.id: d for d in _drafts(profile)}
    existing = {f.id: f for f in session.exec(select(ProfileFact)).all()}
    raw_lines = [line.strip() for line in (source_text or "").splitlines() if line.strip()]
    folded_lines = [_fold(line) for line in raw_lines]
    evidence_at: dict[str, int] = {}
    now = datetime.now(UTC)
    added = updated = deactivated = 0

    for fact_id, draft in drafts.items():
        parent_id = make_fact_id(*draft.parent_key) if draft.parent_key else None
        fact = existing.get(fact_id)
        if fact is None:
            fact = ProfileFact(id=fact_id, kind=draft.kind, text=draft.text, path=draft.path,
                               first_profile_version=version, last_profile_version=version)
            added += 1
        elif not fact.active or fact.text != draft.text or fact.path != draft.path:
            updated += 1
        fact.text, fact.path, fact.parent_id = draft.text, draft.path, parent_id
        fact.data, fact.pii, fact.active = draft.data, draft.pii, True
        fact.verified = profile.finalized
        fact.last_profile_version = version
        fact.updated_at = now
        if raw_lines:
            start = evidence_at.get(parent_id, 0) if parent_id else 0
            index = _evidence_index(draft, folded_lines, start)
            if index is not None:
                evidence_at[fact_id] = index
                fact.evidence = raw_lines[index]
        session.add(fact)

    for fact_id, fact in existing.items():
        if fact_id not in drafts and fact.active:
            fact.active = False
            fact.updated_at = now
            session.add(fact)
            deactivated += 1

    session.commit()
    return SyncReport(added=added, updated=updated, deactivated=deactivated, active=len(drafts))


def active_facts(
    kinds: Iterable[FactKind] | None = None, *, include_pii: bool = False
) -> list[ProfileFact]:
    query = select(ProfileFact).where(col(ProfileFact.active).is_(True))
    if kinds is not None:
        query = query.where(col(ProfileFact.kind).in_(list(kinds)))
    if not include_pii:
        query = query.where(col(ProfileFact.pii).is_(False))
    with get_session() as session:
        return list(session.exec(query.order_by(col(ProfileFact.path))).all())


def format_facts_for_prompt(facts: Iterable[ProfileFact]) -> str:
    """One fact per line: `[Fxxxxxxxxxx] (kind) text`. PII must be filtered out first."""
    lines = []
    for fact in facts:
        if fact.pii:
            raise ValueError(f"refusing to put PII fact {fact.id} ({fact.path}) in a prompt")
        lines.append(f"[{fact.id}] ({fact.kind.value}) {fact.text}")
    return "\n".join(lines)


def unknown_citations(cited: Iterable[str], allowed: Iterable[str]) -> list[str]:
    """Cited ids that are malformed or not among the facts the LLM was shown."""
    shown = set(allowed)
    return sorted({c for c in cited if not _ID_RE.match(c) or c not in shown})
