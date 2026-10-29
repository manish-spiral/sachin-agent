"""Check generated documents against the facts they cite.

Layer 3 of the hallucination defense (after structured facts and prompt rules).
Deterministic checks run first and catch the common fabrications for free —
unknown or misattributed facts, invented numbers, technologies the cited facts
never mention. Only a draft that passes them gets the (paid) LLM entailment
check, which catches subtler overstatement.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ConfigDict

from matcher.llm import StructuredLLM
from matcher.prompts import VERIFY_SYSTEM_PROMPT
from matcher.resume import FactBook, Line, ResumePlan
from shared.llm import Task
from shared.schemas.profile_fact import FactKind

# A number and the word glued to or following it: "25k", "3M", "3 million", "800ms".
_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)*)(\s?)([a-z]+)?", re.I)
_SUFFIX_MAGNITUDE = {"k": 1e3, "m": 1e6, "b": 1e9}  # glued: "25k", "3M"
_WORD_MAGNITUDE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
# Short terms ("Go", "AWS", "C") are matched case-sensitively so the verb "go"
# or the letter "c" in prose never counts as a technology claim.
_CASE_SENSITIVE_MAX_LEN = 3


@dataclass(frozen=True)
class Violation:
    where: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def numbers_in(text: str) -> set[str]:
    """Numeric values, so "25k", "25,000" and "25 thousand" are one claim.
    Units ("800ms", "2 TB") are not magnitudes and leave the value as is."""
    values = set()
    for digits, space, word in _NUMBER_RE.findall(text):
        number = float(digits.replace(",", ""))
        word = word.lower()
        if not space and word in _SUFFIX_MAGNITUDE:
            number *= _SUFFIX_MAGNITUDE[word]
        elif word in _WORD_MAGNITUDE:
            number *= _WORD_MAGNITUDE[word]
        values.add(f"{number:g}")
    return values


def mentions(text: str, term: str) -> bool:
    flags = 0 if len(term) <= _CASE_SENSITIVE_MAX_LEN else re.I
    return re.search(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", text, flags) is not None


def technology_vocabulary(book: FactBook, requirement_terms: Iterable[str]) -> set[str]:
    """Terms that, if named, are a checkable claim: every skill/technology the
    profile lists, plus job terms that look like technologies (contain a
    capital, digit or symbol — "PostgreSQL", "gRPC", "C++", "CI/CD").

    Lowercase job phrases ("communication", "scalable systems") are left to
    the LLM check; flagging them here would reject honest rephrasing.
    """
    vocab = {
        str(f.data.get("skill") or f.data.get("technology"))
        for f in book.kind(FactKind.SKILL, FactKind.TECHNOLOGY)
    }
    for term in requirement_terms:
        term = term.strip()
        if 1 < len(term) <= 30 and " " not in term and re.search(r"[A-Z0-9+#/.]", term):
            vocab.add(term)
    return {v for v in vocab if v and v != "None"}


def check_line(
    where: str,
    line: Line,
    book: FactBook,
    *,
    vocabulary: set[str],
    allowed: set[FactKind] | None = None,
    parent_id: str | None = None,
) -> list[Violation]:
    out: list[Violation] = []
    if not line.fact_ids:
        return [Violation(where, "uncited", f"{line.text!r} cites no facts")]

    cited = []
    for fid in line.fact_ids:
        fact = book.facts.get(fid)
        if fact is None:
            out.append(Violation(where, "unknown_fact", f"cites {fid}, which is not a candidate fact"))
        elif fact.pii:
            out.append(Violation(where, "pii_fact", f"cites contact detail {fid}"))
        elif allowed is not None and fact.kind not in allowed:
            out.append(Violation(where, "wrong_kind", f"cites {fid} ({fact.kind.value}), not allowed here"))
        elif parent_id is not None and fid != parent_id and fact.parent_id != parent_id:
            out.append(Violation(where, "misattributed", f"cites {fid} from a different role: {fact.text!r}"))
        else:
            cited.append(fact)

    source = " ".join(f.text for f in cited)
    invented_numbers = numbers_in(line.text) - numbers_in(source)
    if invented_numbers:
        out.append(Violation(where, "invented_number",
                             f"{', '.join(sorted(invented_numbers))} not in cited facts: {line.text!r}"))
    invented_terms = sorted(t for t in vocabulary if mentions(line.text, t) and not mentions(source, t))
    if invented_terms:
        out.append(Violation(where, "invented_term",
                             f"names {', '.join(invented_terms)}, which cited facts don't: {line.text!r}"))
    return out


_ROLE_KINDS = {FactKind.EXPERIENCE, FactKind.EXPERIENCE_BULLET, FactKind.TECHNOLOGY}


def check_resume_plan(plan: ResumePlan, book: FactBook, vocabulary: set[str]) -> list[Violation]:
    out: list[Violation] = []
    experiences = {f.id: f for f in book.kind(FactKind.EXPERIENCE)}

    seen: set[str] = set()
    for i, role in enumerate(plan.roles):
        exp = experiences.get(role.experience_fact_id)
        if exp is None:
            out.append(Violation(f"roles[{i}]", "unknown_role",
                                 f"{role.experience_fact_id} is not an experience fact"))
            continue
        if exp.id in seen:
            out.append(Violation(f"roles[{i}]", "duplicate_role", f"{exp.text!r} appears twice"))
        seen.add(exp.id)
        if not role.bullets and book.children(exp.id, FactKind.EXPERIENCE_BULLET):
            out.append(Violation(f"roles[{i}]", "empty_role", f"{exp.text!r} has no bullets"))
        for j, bullet in enumerate(role.bullets):
            where = f"roles[{i}] ({exp.data.get('company')}) bullet {j + 1}"
            out += check_line(where, bullet, book, vocabulary=vocabulary, allowed=_ROLE_KINDS, parent_id=exp.id)
            if not any(book.facts.get(f) and book.facts[f].kind == FactKind.EXPERIENCE_BULLET
                       for f in bullet.fact_ids):
                out.append(Violation(where, "no_achievement", "cites no experience_bullet fact"))

    for exp_id in experiences.keys() - seen:
        out.append(Violation("roles", "missing_role", f"{experiences[exp_id].text!r} was left out"))

    for i, line in enumerate(plan.summary):
        out += check_line(f"summary sentence {i + 1}", line, book, vocabulary=vocabulary)

    for fid in plan.skill_fact_ids:
        fact = book.facts.get(fid)
        if fact is None or fact.kind not in {FactKind.SKILL, FactKind.TECHNOLOGY}:
            out.append(Violation("skills", "unknown_skill", f"{fid} is not a skill or technology fact"))
    return out


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    supported: bool
    unsupported: str


class Verdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[Verdict]


@dataclass(frozen=True)
class Statement:
    where: str
    text: str
    sources: list[str]


def llm_check(statements: list[Statement], llm: StructuredLLM) -> list[Violation]:
    """Entailment check of every statement against its own sources, in one call."""
    if not statements:
        return []
    blocks = []
    for i, st in enumerate(statements, start=1):
        sources = "\n".join(f"   - {s}" for s in st.sources) or "   (none)"
        blocks.append(f"{i}. statement: {st.text}\n   sources:\n{sources}")
    result = llm(Task.VERIFY, VERIFY_SYSTEM_PROMPT, "\n\n".join(blocks), Verdicts)

    by_index = {v.index: v for v in result.verdicts}
    out = []
    for i, st in enumerate(statements, start=1):
        verdict = by_index.get(i)
        if verdict is None:
            out.append(Violation(st.where, "unverified", "fact-checker returned no verdict"))
        elif not verdict.supported:
            out.append(Violation(st.where, "unsupported", f"{verdict.unsupported!r} is not supported by its facts"))
    return out


def plan_statements(plan: ResumePlan, book: FactBook) -> list[Statement]:
    lines: list[tuple[str, Line]] = [(f"summary sentence {i + 1}", line) for i, line in enumerate(plan.summary)]
    for i, role in enumerate(plan.roles):
        lines += [(f"roles[{i}] bullet {j + 1}", b) for j, b in enumerate(role.bullets)]
    return [
        Statement(where, line.text, [book.facts[f].text for f in line.fact_ids if f in book.facts])
        for where, line in lines
    ]
