"""Compare LLM scores with the user's own judgment and tune rubric weights.

Labels live in data/calibration.json, tuned weights in data/scoring_weights.json.
"Agreement" means the overall score is within 1 point of the manual score.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from sqlmodel import col, select

from matcher.scoring import overall_score
from shared.db.engine import get_session
from shared.paths import calibration_path, scoring_weights_path
from shared.schemas.job import Job
from shared.schemas.score import Dimension, ScoreBreakdown, ScoreMethod, StoredScore

AGREEMENT_TOLERANCE = 1.0
FIT_ITERATIONS = 3000
FIT_STEP = 0.002


@dataclass
class Label:
    job_id: str
    manual: int
    breakdown: dict

    def scored(self) -> ScoreBreakdown:
        return ScoreBreakdown.model_validate(self.breakdown)


def load_labels() -> list[Label]:
    path = calibration_path()
    if not path.exists():
        return []
    return [Label(**row) for row in json.loads(path.read_text(encoding="utf-8"))]


def save_labels(labels: list[Label]) -> None:
    path = calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(label) for label in labels], indent=2), encoding="utf-8")


def save_weights(weights: dict[str, float]) -> None:
    path = scoring_weights_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(weights, indent=2), encoding="utf-8")


def sample_for_labeling(n: int, exclude: set[str]) -> list[Job]:
    """LLM-scored jobs spread evenly across the score range, so calibration
    covers the bad and middling fits, not just the top of the queue."""
    with get_session() as session:
        jobs = list(
            session.exec(
                select(Job).where(col(Job.score).is_not(None)).order_by(col(Job.score))
            ).all()
        )
    pool = [
        j
        for j in jobs
        if j.id not in exclude
        and j.score_breakdown
        and j.score_breakdown.get("method") == ScoreMethod.LLM
    ]
    if len(pool) <= n:
        return pool
    step = (len(pool) - 1) / (n - 1) if n > 1 else 0
    return [pool[round(i * step)] for i in range(n)]


def label_from_job(job: Job, manual: int) -> Label:
    stored = StoredScore.model_validate(job.score_breakdown)
    assert stored.breakdown is not None
    return Label(job_id=job.id, manual=manual, breakdown=stored.breakdown.model_dump(mode="json"))


@dataclass(frozen=True)
class CalibrationResult:
    n: int
    agreement: float
    mean_abs_error: float


def evaluate(labels: list[Label], weights: dict[str, float]) -> CalibrationResult:
    if not labels:
        return CalibrationResult(n=0, agreement=0.0, mean_abs_error=0.0)
    errors = [abs(overall_score(label.scored(), weights) - label.manual) for label in labels]
    return CalibrationResult(
        n=len(labels),
        agreement=sum(e <= AGREEMENT_TOLERANCE for e in errors) / len(errors),
        mean_abs_error=sum(errors) / len(errors),
    )


def _project_to_simplex(values: list[float]) -> list[float]:
    """Euclidean projection onto {w >= 0, sum(w) = 1} (Duchi et al., 2008)."""
    ordered = sorted(values, reverse=True)
    cumulative = 0.0
    theta = 0.0
    for i, v in enumerate(ordered, start=1):
        cumulative += v
        candidate = (cumulative - 1) / i
        if v - candidate > 0:
            theta = candidate
    return [max(v - theta, 0.0) for v in values]


def fit_weights(labels: list[Label], start: dict[str, float]) -> dict[str, float]:
    """Least-squares weights on the simplex via projected gradient descent.

    Six weights and a few dozen labels: a closed-form solver would be overkill
    and would need numpy.
    """
    dims = list(Dimension)
    rows = [[getattr(label.scored(), d).score for d in dims] for label in labels]
    targets = [label.manual for label in labels]
    total = sum(start.values())
    w = [start[d] / total for d in dims]
    for _ in range(FIT_ITERATIONS):
        grad = [0.0] * len(dims)
        for x, y in zip(rows, targets, strict=True):
            residual = sum(wi * xi for wi, xi in zip(w, x, strict=True)) - y
            for i, xi in enumerate(x):
                grad[i] += 2 * residual * xi / len(rows)
        w = _project_to_simplex([wi - FIT_STEP * g for wi, g in zip(w, grad, strict=True)])
    return {d.value: round(wi, 3) for d, wi in zip(dims, w, strict=True)}
