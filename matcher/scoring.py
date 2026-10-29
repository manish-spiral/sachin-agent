"""Score NEW jobs 1–5 against the finalized profile.

The LLM scores each rubric dimension; the overall score is a weighted mean
computed here, so calibration tunes weights without touching the prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from loguru import logger
from rapidfuzz import fuzz
from sqlmodel import col, select

from matcher.llm import parse_structured
from matcher.prompts import SCORE_PROMPT_VERSION, SCORE_SYSTEM_PROMPT, SCORE_USER_TEMPLATE
from shared.db.engine import get_session
from shared.db.failures import domain_from_url, emit_failure
from shared.killswitch import is_killed
from shared.llm import Task, resolve
from shared.paths import scoring_weights_path
from shared.profile.facts import active_facts, format_facts_for_prompt, unknown_citations
from shared.profile.store import load_current_profile
from shared.schemas.failure import FailureClass
from shared.schemas.job import Job, JobStatus
from shared.schemas.job_transitions import transition
from shared.schemas.profile import Profile
from shared.schemas.profile_fact import ProfileFact
from shared.schemas.score import Dimension, ScoreBreakdown, ScoreMethod, StoredScore

DEFAULT_WEIGHTS: dict[str, float] = {
    Dimension.ROLE_MATCH: 0.30,
    Dimension.SKILLS_OVERLAP: 0.25,
    Dimension.SENIORITY: 0.15,
    Dimension.LOCATION: 0.15,
    Dimension.VISA: 0.10,
    Dimension.COMPENSATION: 0.05,
}
# A job you cannot legally or physically take is not "pretty good" because the
# skills line up; these dimensions cap the overall score when they score 1.
DEALBREAKERS = (Dimension.LOCATION, Dimension.VISA)
DEALBREAKER_CAP = 1.5
PREFILTER_SCORE = 1.0
# The prefilter only drops titles without the role noun of any target title
# (sales roles for an engineer) to save LLM calls; nuance is the LLM's job.
# Two rules that failed on real data: character-level fuzzy scores ("writer
# content marketing" scores 50 against "backend engineer" on letters alone),
# and any shared word ("Account Executive, Platforms" matched "Platform
# Engineer"). Fuzzy matching remains only as a fallback for spelling variants.
TITLE_FUZZY_FALLBACK = 80
# Level and filler words: sharing "senior" says nothing about the role.
_GENERIC_TITLE_WORDS = {
    "senior", "sr", "junior", "jr", "staff", "principal", "lead", "head", "chief",
    "associate", "intern", "trainee", "i", "ii", "iii", "iv", "of", "and", "the",
    "for", "to", "in", "at", "with", "new", "grad", "remote", "hybrid",
}
_TITLE_SYNONYMS = {"developer": "engineer", "programmer": "engineer", "engineering": "engineer", "dev": "engineer"}
DESCRIPTION_CHARS = 6000


class Scorer(Protocol):
    model: str

    def __call__(self, system: str, user: str) -> ScoreBreakdown: ...


class ProfileNotReady(RuntimeError):
    pass


@dataclass
class ScoringReport:
    scored: int = 0
    prefiltered: int = 0
    failed: int = 0
    # Jobs whose reasons cited facts that don't exist (scored, but flagged).
    citation_issues: int = 0
    # Set when the run stopped early (e.g. the provider's daily quota ran out).
    stopped: str | None = None


@dataclass(frozen=True)
class Candidate:
    """The finalized profile plus the non-PII facts the LLM is allowed to see."""

    profile: Profile
    version: int
    facts: list[ProfileFact] = field(default_factory=list)


def load_weights() -> dict[str, float]:
    path = scoring_weights_path()
    if not path.exists():
        return dict(DEFAULT_WEIGHTS)
    stored = json.loads(path.read_text(encoding="utf-8"))
    return {d.value: float(stored.get(d.value, DEFAULT_WEIGHTS[d])) for d in Dimension}


def overall_score(breakdown: ScoreBreakdown, weights: dict[str, float]) -> float:
    total = sum(weights.values())
    value = sum(getattr(breakdown, d).score * weights[d] for d in Dimension) / total
    if any(getattr(breakdown, d).score == 1 for d in DEALBREAKERS):
        value = min(value, DEALBREAKER_CAP)
    return round(value, 2)


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _words(title: str) -> list[str]:
    words = []
    for word in re.findall(r"[a-z0-9]+", title.lower()):
        word = _TITLE_SYNONYMS.get(word, word)
        words.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return words


def _matches_target(title: str, target: str) -> bool:
    """The target's head noun ("engineer" in "Backend Engineer") must be in the
    title. A generic head ("staff" in "Member of Technical Staff") is no signal
    ("Chief of Staff"), so then the whole target phrase must appear."""
    target_words = _words(target)
    if not target_words:
        return False
    head = target_words[-1]
    if head in _GENERIC_TITLE_WORDS:
        return " ".join(target_words) in " ".join(_words(title))
    return head in _words(title)


def prefilter(job: Job, profile: Profile) -> str | None:
    """Return why a job needs no LLM call, or None if it should be scored."""
    company = job.company_norm or _alnum(job.company)
    for excluded in profile.preferences.excluded_companies:
        # "Meta Platforms, Inc." in the profile must exclude company_norm "meta".
        if company and _alnum(excluded).startswith(company):
            return f"company {job.company!r} is in preferences.excluded_companies"

    targets = profile.preferences.target_titles
    if targets:
        title = job.title_norm or job.title.lower()
        if not any(_matches_target(title, t) for t in targets):
            best = max(fuzz.token_set_ratio(title, t.lower()) for t in targets)
            if best < TITLE_FUZZY_FALLBACK:
                return f"title {job.title!r} has no target role noun ({', '.join(targets)})"
    return None


def priority(job: Job, profile: Profile) -> tuple[int, datetime]:
    """Cheap ordering for LLM calls: with a daily request cap, spend it on jobs
    in a place the candidate can work first, newest first within that."""
    places = set((job.location_norm or "").split(" | "))
    preferred = {loc.strip().lower() for loc in profile.preferences.preferred_locations}
    if places & preferred:
        location = 2
    elif "remote" in places and profile.preferences.remote_ok:
        location = 1
    else:
        location = 0
    seen = job.posted_at or job.first_seen_at
    return location, seen.replace(tzinfo=seen.tzinfo or UTC)


def build_user_prompt(job: Job, facts: list[ProfileFact]) -> str:
    if job.salary_min or job.salary_max:
        salary = f"{job.salary_min or '?'}–{job.salary_max or '?'} {job.salary_currency or ''}".strip()
    else:
        salary = "not stated"
    return SCORE_USER_TEMPLATE.format(
        today=datetime.now(UTC).date().isoformat(),
        facts=format_facts_for_prompt(facts),
        title=job.title,
        company=job.company,
        location=job.location_raw or "not stated",
        remote={True: "yes", False: "no", None: "unknown"}[job.remote],
        salary=salary,
        description=job.description_raw[:DESCRIPTION_CHARS],
    )


class LLMScorer:
    def __init__(self) -> None:
        self.config = resolve(Task.SCORING)
        self.model = f"{self.config.provider.value}:{self.config.model}"

    def __call__(self, system: str, user: str) -> ScoreBreakdown:
        return parse_structured(system, user, ScoreBreakdown, config=self.config)


def load_candidate() -> Candidate:
    current = load_current_profile()
    if current is None:
        raise ProfileNotReady("No profile found. Run `sachin-agent-profile import <cv>` first.")
    profile, version = current
    if not profile.finalized:
        raise ProfileNotReady(
            "Profile is not finalized. Review it, then set `finalized: true` "
            "(`sachin-agent-profile validate` lists what's missing)."
        )
    return Candidate(profile=profile, version=version, facts=active_facts(include_pii=False))


def score_job(
    job: Job,
    candidate: Candidate,
    *,
    scorer: Scorer,
    weights: dict[str, float],
) -> StoredScore:
    reason = prefilter(job, candidate.profile)
    if reason is not None:
        return StoredScore(
            method=ScoreMethod.PREFILTER,
            overall=PREFILTER_SCORE,
            prefilter_reason=reason,
            profile_version=candidate.version,
        )
    breakdown = scorer(SCORE_SYSTEM_PROMPT, build_user_prompt(job, candidate.facts))
    cited = [fact_id for d in Dimension for fact_id in getattr(breakdown, d).evidence]
    return StoredScore(
        method=ScoreMethod.LLM,
        overall=overall_score(breakdown, weights),
        breakdown=breakdown,
        weights=weights,
        prompt_version=SCORE_PROMPT_VERSION,
        model=scorer.model,
        profile_version=candidate.version,
        citation_issues=unknown_citations(cited, allowed=(f.id for f in candidate.facts)),
    )


def score_pending_jobs(
    *,
    limit: int | None = None,
    rescore: bool = False,
    scorer: Scorer | None = None,
) -> ScoringReport:
    """Score NEW (and with `rescore`, already SCORED) non-duplicate jobs.

    Prefiltered jobs cost nothing and are always processed. `limit` caps LLM
    calls, spent in `priority` order.
    """
    candidate = load_candidate()
    scorer = scorer or LLMScorer()
    weights = load_weights()
    statuses = [JobStatus.NEW, JobStatus.SCORED] if rescore else [JobStatus.NEW]
    report = ScoringReport()

    with get_session() as session:
        query = select(Job).where(col(Job.status).in_(statuses), col(Job.duplicate_of).is_(None))
        free: list[Job] = []
        paid: list[Job] = []
        for job in session.exec(query).all():
            (paid if prefilter(job, candidate.profile) is None else free).append(job)
        paid.sort(key=lambda j: priority(j, candidate.profile), reverse=True)
        for job in free + paid[:limit]:
            if is_killed():
                report.stopped = "kill switch"
                break
            log = logger.bind(job_id=job.id)
            try:
                stored = score_job(job, candidate, scorer=scorer, weights=weights)
            except Exception as exc:
                log.error(f"scoring failed for {job.title!r} @ {job.company}: {exc}")
                emit_failure(
                    source_module="matcher",
                    phase="score",
                    error=exc,
                    failure_class=getattr(exc, "failure_class", FailureClass.UNKNOWN),
                    domain=domain_from_url(job.url),
                    job_id=job.id,
                    context={"prompt_version": SCORE_PROMPT_VERSION},
                )
                report.failed += 1
                if getattr(exc, "failure_class", None) == FailureClass.RATE_LIMIT:
                    # The SDK already waited out short limits; what is left is
                    # a quota (e.g. tokens/day). Every further call would fail.
                    report.stopped = f"rate limit: {exc}"[:300]
                    break
                continue

            job.score = stored.overall
            job.score_breakdown = stored.model_dump(mode="json")
            transition(job, JobStatus.SCORED)
            session.add(job)
            session.commit()
            if stored.method == ScoreMethod.PREFILTER:
                report.prefiltered += 1
            else:
                report.scored += 1
                log.info(f"scored {stored.overall:.2f}: {job.title!r} @ {job.company}")
            if stored.citation_issues:
                report.citation_issues += 1
                log.warning(f"reasons cite unknown facts {stored.citation_issues}")
    return report
