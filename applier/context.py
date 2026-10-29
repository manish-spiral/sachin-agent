"""Everything the applier may type into a form, built only from profile facts.

Answers come from (in order): fixed rules over identity / work-authorization
facts, then the human-maintained answers bank (Profile.answers). Anything else
is "unknown" and goes to a human — the applier never makes up an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from rapidfuzz import fuzz

from shared.schemas.profile_fact import FactKind, ProfileFact

ANSWER_MATCH_THRESHOLD = 85
OPTION_MATCH_THRESHOLD = 70

# Demographic / EEO questions are voluntary; the agent never volunteers them.
_DEMOGRAPHIC = re.compile(
    r"gender|race|racial|ethnic|sexual orientation|transgender|disability|veteran|pronoun", re.I
)
# Legal acknowledgements and consents are policy decisions, never automated.
_CONSENT = re.compile(r"acknowledg|i agree|i consent|consent to|privacy (policy|notice)|terms", re.I)
_DECLINE_OPTION = re.compile(r"decline|prefer not|don.?t wish|not to (say|disclose|answer)", re.I)


class AnswerSource(StrEnum):
    IDENTITY = "identity"
    WORK_AUTHORIZATION = "work_authorization"
    ANSWERS_BANK = "answers_bank"
    DECLINE = "decline"


@dataclass(frozen=True)
class Answer:
    value: str
    source: AnswerSource
    fact_id: str | None = None


class Unanswerable(StrEnum):
    UNKNOWN = "unknown"  # no fact answers it: ask the human
    CONSENT = "consent"  # a legal acknowledgement: the human must decide
    SKIP = "skip"  # optional demographic question with no decline option


@dataclass
class ApplicantContext:
    facts: list[ProfileFact]
    resume_path: Path | None = None
    cover_letter_path: Path | None = None
    _by_path: dict[str, ProfileFact] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._by_path = {f.path: f for f in self.facts}

    def identity(self, name: str) -> str:
        fact = self._by_path.get(f"identity.{name}")
        return str(fact.data.get("value", "")) if fact else ""

    def value_for(self, intent: str) -> str:
        """Standard field intents ("first_name", "email", ...) → identity facts."""
        return self.identity(intent)

    def _auth(self) -> tuple[bool | None, list[str], str | None]:
        sponsorship = self._by_path.get("work_authorization.requires_sponsorship")
        countries = [
            str(f.data["country"]) for f in self.facts
            if f.kind == FactKind.WORK_AUTHORIZATION and "country" in f.data
        ]
        needs = None if sponsorship is None else sponsorship.text.startswith("Requires")
        return needs, countries, sponsorship.id if sponsorship else None

    def answer(self, question: str, options: list[str] | None = None) -> Answer | Unanswerable:
        q = question.lower()
        if _DEMOGRAPHIC.search(q):
            decline = next((o for o in options or [] if _DECLINE_OPTION.search(o)), None)
            return Answer(decline, AnswerSource.DECLINE) if decline else Unanswerable.SKIP
        if _CONSENT.search(q):
            return Unanswerable.CONSENT

        for name, pattern in (("linkedin", r"linkedin"), ("github", r"github"),
                              ("portfolio", r"website|portfolio")):
            if re.search(pattern, q):
                value = self.identity(name)
                if value:
                    fact = self._by_path.get(f"identity.{name}")
                    return self._choose(Answer(value, AnswerSource.IDENTITY, fact.id if fact else None), options)

        needs, countries, fact_id = self._auth()
        if needs is not None and re.search(r"sponsor", q):
            return self._choose(Answer("Yes" if needs else "No", AnswerSource.WORK_AUTHORIZATION, fact_id), options)

        best: tuple[float, ProfileFact] | None = None
        for fact in self.facts:
            if fact.kind != FactKind.ANSWER:
                continue
            score = fuzz.token_set_ratio(q, str(fact.data["question"]).lower())
            if score >= ANSWER_MATCH_THRESHOLD and (best is None or score > best[0]):
                best = (score, fact)
        if best is not None:
            return self._choose(Answer(str(best[1].data["answer"]), AnswerSource.ANSWERS_BANK, best[1].id), options)
        return Unanswerable.UNKNOWN

    @staticmethod
    def _choose(answer: Answer, options: list[str] | None) -> Answer | Unanswerable:
        """For a dropdown, the answer must map onto one of its options."""
        if not options:
            return answer
        scored = [(fuzz.WRatio(answer.value.lower(), o.lower()), o) for o in options]
        score, option = max(scored)
        if score < OPTION_MATCH_THRESHOLD:
            return Unanswerable.UNKNOWN
        return Answer(option, answer.source, answer.fact_id)
