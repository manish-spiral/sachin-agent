"""Check an extracted profile against its source CV text.

Two failure modes, both seen live: the LLM invents a value that is not in the
CV, or silently drops a whole section (e.g. returns no skills for a CV that
has a Skills line). Purely textual, so it cannot prove faithfulness — but it
catches the cheap, common errors before a human reviews the draft.
"""

from __future__ import annotations

import re
import unicodedata

from shared.schemas.profile import Profile

# Section headings at the start of a line, e.g. "Skills:", "Technical Skills".
_SECTIONS = {
    "skills": re.compile(r"^\s*(?:technical\s+|key\s+|core\s+)?skills\b", re.I | re.M),
    "experience": re.compile(r"^\s*(?:work\s+|professional\s+)?experience\b", re.I | re.M),
    "education": re.compile(r"^\s*education\b", re.I | re.M),
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return " ".join(folded.split())


def _claims(profile: Profile) -> list[tuple[str, str]]:
    """(path, value) for every short factual string that must appear verbatim."""
    claims: list[tuple[str, str]] = []
    for i, exp in enumerate(profile.experience):
        claims += [(f"experience[{i}].company", exp.company), (f"experience[{i}].title", exp.title)]
    for i, edu in enumerate(profile.education):
        claims.append((f"education[{i}].institution", edu.institution))
    for i, cert in enumerate(profile.certifications):
        claims.append((f"certifications[{i}].name", cert.name))
    for i, project in enumerate(profile.projects):
        claims.append((f"projects[{i}].name", project.name))
    for group, values in profile.skills.model_dump().items():
        claims += [(f"skills.{group}", v) for v in values]
    return [(path, value) for path, value in claims if value.strip()]


def faithfulness_issues(profile: Profile, cv_text: str) -> list[str]:
    text = _fold(cv_text)
    issues = [
        f"possibly invented: {path} {value!r} does not appear in the CV"
        for path, value in _claims(profile)
        if _fold(value) not in text
    ]

    skills = profile.skills
    extracted = {
        "skills": skills.languages or skills.frameworks or skills.tools or skills.soft_skills,
        "experience": profile.experience,
        "education": profile.education,
    }
    issues += [
        f"possibly dropped: the CV has a {name.capitalize()} section but none was extracted"
        for name, pattern in _SECTIONS.items()
        if pattern.search(cv_text) and not extracted[name]
    ]
    return issues
