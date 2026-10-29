"""Phase 5: fit scoring, review and calibration — with a fake LLM."""

from __future__ import annotations

import json

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from matcher.calibrate import (
    Label,
    _project_to_simplex,
    evaluate,
    fit_weights,
    load_labels,
    sample_for_labeling,
)
from matcher.cli import app
from matcher.review import JobNotFound, decide, find_job, review_queue
from matcher.scoring import (
    DEALBREAKER_CAP,
    DEFAULT_WEIGHTS,
    ProfileNotReady,
    build_user_prompt,
    overall_score,
    prefilter,
    score_pending_jobs,
)
from shared.db.engine import get_session
from shared.db.upsert import upsert_job
from shared.paths import calibration_path, scoring_weights_path
from shared.profile.store import save_profile
from shared.schemas.failure import FailureClass, FailureEvent
from shared.schemas.job import Job, JobStatus, make_job_id
from shared.schemas.job_transitions import IllegalTransition
from shared.schemas.profile import Profile
from shared.schemas.score import Dimension, ScoreBreakdown, ScoreMethod, StoredScore


def _profile(**preferences) -> Profile:
    return Profile.model_validate(
        {
            "identity": {
                "first_name": "Sachin",
                "last_name": "T",
                "email": "sachin@example.com",
                "phone": "+91 99999 99999",
                "location": "Bengaluru",
            },
            "preferences": {"target_titles": ["Backend Engineer"], **preferences},
            "experience": [{"company": "Acme", "title": "Backend Engineer", "bullets": ["Built X"]}],
            "skills": {"languages": ["Python"]},
            "finalized": True,
        }
    )


def _breakdown(**scores: int) -> ScoreBreakdown:
    return ScoreBreakdown.model_validate(
        {
            **{d.value: {"score": scores.get(d.value, 3), "reason": f"{d.value} reason"} for d in Dimension},
            "summary": "ok",
        }
    )


class FakeScorer:
    def __init__(self, breakdown: ScoreBreakdown | None = None, fail_on: str | None = None):
        self.breakdown = breakdown or _breakdown(role_match=5, skills_overlap=4)
        self.fail_on = fail_on
        self.prompts: list[str] = []

    def __call__(self, system: str, user: str) -> ScoreBreakdown:
        self.prompts.append(user)
        if self.fail_on and self.fail_on in user:
            from matcher.llm import LLMError

            raise LLMError("boom", failure_class=FailureClass.TRANSIENT)
        return self.breakdown


def _job(ext: str, title: str = "Senior Backend Engineer", company: str = "Globex", **kw) -> Job:
    return Job(
        id=make_job_id("greenhouse:globex", ext),
        source="greenhouse:globex",
        external_id=ext,
        title=title,
        company=company,
        title_norm=title.lower(),
        company_norm=company.lower(),
        description_raw="Python services.",
        url=f"https://boards.greenhouse.io/globex/jobs/{ext}",
        **kw,
    )


def _store(*jobs: Job) -> None:
    with get_session() as s:
        for job in jobs:
            upsert_job(s, job)


def _get(job_id: str) -> Job:
    with get_session() as s:
        job = s.get(Job, job_id)
    assert job is not None
    return job


@pytest.fixture
def ready(fresh_db):
    for path in (scoring_weights_path(), calibration_path()):
        path.unlink(missing_ok=True)
    save_profile(_profile())
    yield
    for path in (scoring_weights_path(), calibration_path()):
        path.unlink(missing_ok=True)


# --- pure scoring ----------------------------------------------------------


def test_overall_is_weighted_mean():
    assert overall_score(_breakdown(), DEFAULT_WEIGHTS) == 3.0
    assert overall_score(_breakdown(role_match=5), DEFAULT_WEIGHTS) == 3.6


def test_dealbreaker_caps_overall():
    strong_but_impossible = _breakdown(role_match=5, skills_overlap=5, seniority=5, visa=1)
    assert overall_score(strong_but_impossible, DEFAULT_WEIGHTS) == DEALBREAKER_CAP


def test_prefilter_excluded_company_uses_suffix_tolerant_match():
    profile = _profile(excluded_companies=["Globex, Inc."])
    assert "excluded" in (prefilter(_job("1"), profile) or "")


def test_prefilter_drops_unrelated_titles_only():
    profile = _profile()
    assert prefilter(_job("1", title="Account Executive"), profile) is not None
    assert prefilter(_job("2", title="Senior Product Designer"), profile) is not None
    assert prefilter(_job("3", title="Staff Backend Engineer, Payments"), profile) is None
    assert prefilter(_job("4", title="Software Engineer"), profile) is None


def test_prompt_has_no_contact_pii():
    prompt = build_user_prompt(_job("1"), _profile())
    assert "sachin@example.com" not in prompt
    assert "99999" not in prompt
    assert "Sachin" not in prompt
    assert "Bengaluru" in prompt  # location is a fit signal, not contact info
    assert "Python services." in prompt


# --- pipeline --------------------------------------------------------------


def test_requires_finalized_profile(fresh_db):
    draft = _profile()
    draft.finalized = False
    save_profile(draft)
    with pytest.raises(ProfileNotReady, match="not finalized"):
        score_pending_jobs(scorer=FakeScorer())


def test_scores_new_jobs_and_is_idempotent(ready):
    good = _job("1")
    unrelated = _job("2", title="Account Executive")
    _store(good, unrelated)
    scorer = FakeScorer()

    report = score_pending_jobs(scorer=scorer)
    again = score_pending_jobs(scorer=scorer)

    assert (report.scored, report.prefiltered, report.failed) == (1, 1, 0)
    assert (again.scored, again.prefiltered) == (0, 0)
    assert len(scorer.prompts) == 1  # the prefiltered job never reached the LLM

    stored = StoredScore.model_validate(_get(good.id).score_breakdown)
    assert _get(good.id).status == JobStatus.SCORED
    assert stored.method == ScoreMethod.LLM
    assert stored.prompt_version == "score_job_v1"
    assert stored.profile_version == 1
    assert stored.breakdown is not None and stored.breakdown.role_match.score == 5

    pre = StoredScore.model_validate(_get(unrelated.id).score_breakdown)
    assert pre.method == ScoreMethod.PREFILTER
    assert _get(unrelated.id).score == 1.0


def test_skips_duplicates(ready):
    canonical = _job("1")
    dup = _job("2", duplicate_of=canonical.id)
    _store(canonical)
    with get_session() as s:
        s.add(dup)
        s.commit()

    score_pending_jobs(scorer=FakeScorer())

    assert _get(dup.id).status == JobStatus.NEW
    assert _get(dup.id).score is None


def test_llm_failure_is_captured_and_job_stays_new(ready):
    ok, bad = _job("1"), _job("2", title="Backend Engineer, Explodes")
    _store(ok, bad)

    report = score_pending_jobs(scorer=FakeScorer(fail_on="Explodes"))

    assert (report.scored, report.failed) == (1, 1)
    assert _get(bad.id).status == JobStatus.NEW
    with get_session() as s:
        failure = s.exec(select(FailureEvent)).one()
    assert failure.source_module == "matcher"
    assert failure.job_id == bad.id
    assert failure.failure_class == FailureClass.TRANSIENT
    assert failure.domain == "boards.greenhouse.io"


def test_rescore_updates_scored_but_not_approved(ready):
    scored, approved = _job("1"), _job("2")
    _store(scored, approved)
    score_pending_jobs(scorer=FakeScorer(_breakdown()))
    decide(approved.id, JobStatus.APPROVED)

    score_pending_jobs(rescore=True, scorer=FakeScorer(_breakdown(role_match=5)))

    assert _get(scored.id).score == 3.6
    assert _get(approved.id).score == 3.0
    assert _get(approved.id).status == JobStatus.APPROVED


def test_custom_weights_file_is_used(ready):
    scoring_weights_path().write_text(json.dumps({d.value: 0.0 for d in Dimension} | {"role_match": 1.0}))
    _store(_job("1"))
    score_pending_jobs(scorer=FakeScorer(_breakdown(role_match=5)))
    assert _get(make_job_id("greenhouse:globex", "1")).score == 5.0


# --- review ----------------------------------------------------------------


def test_review_queue_orders_by_score_and_decide_transitions(ready):
    low, high = _job("1"), _job("2")
    _store(low, high)
    score_pending_jobs(scorer=FakeScorer(_breakdown()))
    with get_session() as s:
        row = s.get(Job, high.id)
        assert row is not None
        row.score = 4.8
        s.add(row)
        s.commit()

    assert [j.id for j in review_queue()] == [high.id, low.id]
    assert [j.id for j in review_queue(min_score=4)] == [high.id]

    decide(high.id, JobStatus.APPROVED)
    assert [j.id for j in review_queue()] == [low.id]
    decide(high.id, JobStatus.APPLIED)
    with pytest.raises(IllegalTransition):
        decide(high.id, JobStatus.REJECTED)


def test_find_job_by_prefix(ready):
    job = _job("1")
    _store(job)
    assert find_job(job.id[:8]).id == job.id
    with pytest.raises(JobNotFound):
        find_job("zzzz")


def test_review_cli_interactive(ready):
    a, b = _job("1"), _job("2")
    _store(a, b)
    score_pending_jobs(scorer=FakeScorer())

    result = CliRunner().invoke(app, ["review"], input="a\nr\n")

    assert result.exit_code == 0, result.output
    assert "role_match" in result.output
    assert "approved: 1  rejected: 1" in result.output
    assert {_get(a.id).status, _get(b.id).status} == {JobStatus.APPROVED, JobStatus.REJECTED}


def test_approve_cli_rejects_illegal_transition(ready):
    job = _job("1")
    _store(job)  # still NEW: NEW → APPROVED is illegal
    result = CliRunner().invoke(app, ["approve", job.id[:12]])
    assert result.exit_code == 1
    assert "illegal transition" in result.output


def test_stats_cli(ready):
    _store(_job("1"), _job("2", title="Account Executive"))
    score_pending_jobs(scorer=FakeScorer())
    result = CliRunner().invoke(app, ["stats"])
    assert result.exit_code == 0, result.output
    assert "scored" in result.output
    assert "llm=1" in result.output and "prefilter=1" in result.output


def test_run_cli_without_profile_explains(fresh_db, monkeypatch):
    monkeypatch.setattr("matcher.scoring.load_profile", lambda: None)
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 1
    assert "No profile found" in result.output


# --- calibration -----------------------------------------------------------


def _label(manual: int, **scores: int) -> Label:
    return Label(job_id=f"j{manual}{scores}", manual=manual, breakdown=_breakdown(**scores).model_dump())


def test_simplex_projection():
    w = _project_to_simplex([0.5, 0.9, -0.2])
    assert abs(sum(w) - 1) < 1e-9
    assert all(x >= 0 for x in w)


def test_fit_weights_learns_what_the_user_cares_about():
    # The user's score tracks location, not role: the fit must move weight there.
    labels = [
        _label(5, location=5, role_match=1),
        _label(1, location=1, role_match=5),
        _label(4, location=4, role_match=2),
        _label(2, location=2, role_match=4),
    ]
    fitted = fit_weights(labels, dict(DEFAULT_WEIGHTS))
    assert fitted["location"] > fitted["role_match"]
    assert evaluate(labels, fitted).mean_abs_error < evaluate(labels, dict(DEFAULT_WEIGHTS)).mean_abs_error


def test_evaluate_agreement_tolerance():
    labels = [_label(3), _label(4), _label(5)]  # every LLM overall is 3.0
    result = evaluate(labels, dict(DEFAULT_WEIGHTS))
    assert result.agreement == pytest.approx(2 / 3)


def test_sample_spreads_across_score_range(ready):
    _store(*[_job(str(i)) for i in range(10)])
    score_pending_jobs(scorer=FakeScorer())
    with get_session() as s:
        for i, job in enumerate(s.exec(select(Job)).all()):
            job.score = float(i)
            s.add(job)
        s.commit()

    picked = sample_for_labeling(3, exclude=set())
    assert [j.score for j in picked] == [0.0, 4.0, 9.0]


def test_calibrate_cli_label_then_report_apply(ready):
    _store(*[_job(str(i)) for i in range(3)])
    score_pending_jobs(scorer=FakeScorer(_breakdown()))
    runner = CliRunner()

    labeled = runner.invoke(app, ["calibrate", "label", "--n", "3"], input="5\ns\n1\n")
    assert labeled.exit_code == 0, labeled.output
    assert "/5" not in labeled.output  # the LLM score is hidden while labeling
    assert [label.manual for label in load_labels()] == [5, 1]

    report = runner.invoke(app, ["calibrate", "report", "--apply"])
    assert report.exit_code == 0, report.output
    assert "agreement" in report.output
