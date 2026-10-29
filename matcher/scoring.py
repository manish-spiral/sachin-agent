"""Score NEW jobs 1–5 against the finalized profile.

The LLM scores each rubric dimension; the overall score is a weighted mean
computed here, so calibration tunes weights without touching the prompt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger
from rapidfuzz import fuzz
from sqlmodel import col, select

from matcher.llm import parse_structured
from matcher.prompts import SCORE_PROMPT_VERSION, SCORE_SYSTEM_PROMPT, SCORE_USER_TEMPLATE
from shared.config import get_settings
from shared.db.engine import get_session
from shared.db.failures import domain_from_url, emit_failure
from shared.paths import scoring_weights_path
from shared.profile.store import list_history, load_profile
from shared.schemas.failure import FailureClass
from shared.schemas.job import Job, JobStatus
from shared.schemas.job_transitions import transition
from shared.schemas.profile import Profile
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
# Deliberately permissive: the prefilter only drops obviously unrelated titles
# (sales roles for an engineer) to save LLM calls; nuance is the LLM's job.
TITLE_PREFILTER_THRESHOLD = 50
DESCRIPTION_CHARS = 6000

Scorer = Callable[[str, str], ScoreBreakdown]


class ProfileNotReady(RuntimeError):
    pass


@dataclass
class ScoringReport:
    scored: int = 0
    prefiltered: int = 0
    failed: int = 0


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
        best = max(fuzz.token_set_ratio(title, t.lower()) for t in targets)
        if best < TITLE_PREFILTER_THRESHOLD:
            return f"title {job.title!r} is unrelated to target titles (best match {best:.0f}/100)"
    return None


def profile_for_prompt(profile: Profile) -> str:
    """The profile minus contact details: scoring needs facts, not PII."""
    data = profile.model_dump(
        mode="json",
        exclude={
            "identity": {"first_name", "last_name", "email", "phone", "linkedin", "github", "portfolio"},
            "finalized": True,
        },
    )
    return json.dumps(data, ensure_ascii=False)


def build_user_prompt(job: Job, profile: Profile) -> str:
    if job.salary_min or job.salary_max:
        salary = f"{job.salary_min or '?'}–{job.salary_max or '?'} {job.salary_currency or ''}".strip()
    else:
        salary = "not stated"
    return SCORE_USER_TEMPLATE.format(
        profile_json=profile_for_prompt(profile),
        title=job.title,
        company=job.company,
        location=job.location_raw or "not stated",
        remote={True: "yes", False: "no", None: "unknown"}[job.remote],
        salary=salary,
        description=job.description_raw[:DESCRIPTION_CHARS],
    )


def default_scorer() -> Scorer:
    model = get_settings().scoring_model

    def score(system: str, user: str) -> ScoreBreakdown:
        return parse_structured(system, user, ScoreBreakdown, model=model)

    return score


def load_ready_profile() -> tuple[Profile, int | None]:
    profile = load_profile()
    if profile is None:
        raise ProfileNotReady("No profile found. Run `sachin-agent-profile import <cv>` first.")
    if not profile.finalized:
        raise ProfileNotReady(
            "Profile is not finalized. Review it, then set `finalized: true` "
            "(`sachin-agent-profile validate` lists what's missing)."
        )
    history = list_history(limit=1)
    return profile, history[0].version if history else None


def score_job(
    job: Job,
    profile: Profile,
    *,
    scorer: Scorer,
    weights: dict[str, float],
    profile_version: int | None,
) -> StoredScore:
    reason = prefilter(job, profile)
    if reason is not None:
        return StoredScore(
            method=ScoreMethod.PREFILTER,
            overall=PREFILTER_SCORE,
            prefilter_reason=reason,
            profile_version=profile_version,
        )
    breakdown = scorer(SCORE_SYSTEM_PROMPT, build_user_prompt(job, profile))
    return StoredScore(
        method=ScoreMethod.LLM,
        overall=overall_score(breakdown, weights),
        breakdown=breakdown,
        weights=weights,
        prompt_version=SCORE_PROMPT_VERSION,
        model=get_settings().scoring_model,
        profile_version=profile_version,
    )


def score_pending_jobs(
    *,
    limit: int | None = None,
    rescore: bool = False,
    scorer: Scorer | None = None,
) -> ScoringReport:
    """Score NEW (and with `rescore`, already SCORED) non-duplicate jobs, newest first."""
    profile, profile_version = load_ready_profile()
    scorer = scorer or default_scorer()
    weights = load_weights()
    statuses = [JobStatus.NEW, JobStatus.SCORED] if rescore else [JobStatus.NEW]
    report = ScoringReport()

    with get_session() as session:
        query = (
            select(Job)
            .where(col(Job.status).in_(statuses), col(Job.duplicate_of).is_(None))
            .order_by(col(Job.posted_at).desc().nulls_last(), col(Job.first_seen_at).desc())
            .limit(limit)
        )
        jobs = list(session.exec(query).all())
        for job in jobs:
            log = logger.bind(job_id=job.id)
            try:
                stored = score_job(
                    job, profile, scorer=scorer, weights=weights, profile_version=profile_version
                )
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
    return report
