"""What the human has taught the applier: consent decisions and answers.

Answers go into the profile's answers bank (and so become citable facts);
consent decisions go into consent_choices, scoped to one domain.
"""

from __future__ import annotations

from sqlmodel import col, select

from shared.db.engine import get_session
from shared.profile.store import load_profile, save_profile
from shared.schemas.consent import ConsentChoice, ConsentKind, make_consent_id
from shared.schemas.profile import Answer


def consents_for(domain: str, kind: ConsentKind | None = None) -> list[ConsentChoice]:
    query = select(ConsentChoice).where(ConsentChoice.domain == domain)
    if kind is not None:
        query = query.where(ConsentChoice.kind == kind)
    with get_session() as s:
        return list(s.exec(query.order_by(col(ConsentChoice.decided_at))).all())


def remember_consent(domain: str, kind: ConsentKind, prompt: str, choice: str) -> ConsentChoice:
    row = ConsentChoice(id=make_consent_id(domain, kind, prompt), domain=domain, kind=kind,
                        prompt=prompt, choice=choice)
    with get_session() as s:
        merged = s.merge(row)
        s.commit()
        s.refresh(merged)
    return merged


def forget_consents(domain: str) -> int:
    with get_session() as s:
        rows = list(s.exec(select(ConsentChoice).where(ConsentChoice.domain == domain)).all())
        for row in rows:
            s.delete(row)
        s.commit()
    return len(rows)


def remember_answer(question: str, answer: str) -> None:
    """Add (or replace) an answers-bank entry; saved as a new profile version."""
    profile = load_profile()
    if profile is None:
        raise RuntimeError("no profile to teach")
    kept = [a for a in profile.answers if a.question.strip().lower() != question.strip().lower()]
    profile.answers = [*kept, Answer(question=question, answer=answer)]
    save_profile(profile, source="teaching", notes=f"answered: {question[:80]}")
