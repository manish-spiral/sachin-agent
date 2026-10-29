"""Run the whole pipeline: scrape → score → (auto-approve) → tailor → apply.

Each stage is a subprocess of that module's own CLI. The orchestrator never
imports scraper, matcher or applier code (enforced by tests/test_boundaries.py),
so the three modules stay independent and only meet in the database.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import col, select

from shared.config import Settings, get_settings
from shared.db.engine import get_session
from shared.killswitch import is_killed
from shared.notify import notify
from shared.schemas.application import Application
from shared.schemas.failure import FailureEvent
from shared.schemas.job import Job
from shared.schemas.pipeline_run import PipelineRun, RunStatus

STAGE_TIMEOUT_SECONDS = 60 * 60
OUTPUT_TAIL_CHARS = 2000


@dataclass(frozen=True)
class Stage:
    name: str
    argv: list[str]


@dataclass(frozen=True)
class StageResult:
    exit_code: int
    output: str


StageRunner = Callable[[Stage], StageResult]


def _module(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", f"{module}.cli", *args]


def build_stages(settings: Settings, *, scrape: bool = True) -> list[Stage]:
    stages = []
    if scrape:
        stages.append(Stage("scrape", _module("scraper", "run")))
    stages.append(Stage("score", _module("matcher", "run", "--limit", str(settings.score_llm_calls_per_run))))
    if settings.auto_approve_min_score is not None:
        argv = _module("matcher", "auto-approve", "--min-score", str(settings.auto_approve_min_score),
                       "--limit", str(settings.max_applications_per_day))
        for company in settings.exclude_companies:
            argv += ["--exclude-company", company]
        for keyword in settings.exclude_title_keywords:
            argv += ["--exclude-title", keyword]
        stages.append(Stage("approve", argv))
    stages.append(Stage("tailor", _module("matcher", "tailor", "--approved", str(settings.tailor_per_run))))
    stages.append(Stage("apply", _module("applier", "run-approved")))
    return stages


def subprocess_runner(stage: Stage) -> StageResult:
    try:
        done = subprocess.run(stage.argv, capture_output=True, text=True, timeout=STAGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return StageResult(124, f"timed out after {exc.timeout}s")
    return StageResult(done.returncode, (done.stdout or "") + (done.stderr or ""))


def snapshot_counts() -> dict[str, dict[str, int]]:
    with get_session() as s:
        jobs = Counter(j.status.value for j in s.exec(select(Job).where(col(Job.duplicate_of).is_(None))).all())
        apps = Counter(
            a.status.value + (f"/{a.pause_reason.value}" if a.pause_reason else "")
            for a in s.exec(select(Application)).all()
        )
        failures = Counter(f.source_module for f in s.exec(select(FailureEvent)).all())
    return {"jobs": dict(jobs), "applications": dict(apps), "failures": dict(failures)}


def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = before.keys() | after.keys()
    return {k: after.get(k, 0) - before.get(k, 0) for k in sorted(keys) if after.get(k, 0) != before.get(k, 0)}


def summarize(before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]) -> dict:
    return {
        "jobs_total": sum(after["jobs"].values()),
        "jobs_change": _delta(before["jobs"], after["jobs"]),
        "applications_now": after["applications"],
        "applications_change": _delta(before["applications"], after["applications"]),
        "new_failures": _delta(before["failures"], after["failures"]),
    }


def format_summary(run: PipelineRun) -> str:
    lines = [f"sachin-agent run {run.id[:8]}: {run.status.value}"]
    for stage in run.stages:
        mark = "✓" if stage["exit_code"] == 0 else ("⏭" if stage["exit_code"] is None else "✗")
        lines.append(f"{mark} {stage['name']} ({stage['seconds']:.0f}s)")
    s = run.summary
    lines.append(f"jobs: {s.get('jobs_total', 0)} unique; change {s.get('jobs_change') or 'none'}")
    lines.append(f"applications change: {s.get('applications_change') or 'none'}")
    paused = {k: v for k, v in s.get("applications_now", {}).items() if k.startswith("paused")}
    if paused:
        lines.append(f"waiting for you: {paused}  →  sachin-agent-applier list --status paused")
    lines.append(f"new failures: {s.get('new_failures') or 'none'}")
    return "\n".join(lines)


def _save(run: PipelineRun) -> None:
    with get_session() as s:
        s.merge(run)
        s.commit()


def run_pipeline(
    stages: list[Stage], *, runner: StageRunner = subprocess_runner, trigger: str = "manual",
    send: bool = True,
) -> PipelineRun:
    run = PipelineRun(id=str(uuid.uuid4()), trigger=trigger)
    _save(run)
    before = snapshot_counts()
    failed = 0
    for stage in stages:
        if is_killed():
            run.status = RunStatus.KILLED
            run.stages.append({"name": stage.name, "argv": stage.argv, "exit_code": None,
                               "seconds": 0.0, "output_tail": "skipped: kill switch"})
            continue
        started = time.monotonic()
        logger.info(f"stage {stage.name}: {' '.join(stage.argv[1:])}")
        result = runner(stage)
        seconds = time.monotonic() - started
        run.stages.append({"name": stage.name, "argv": stage.argv, "exit_code": result.exit_code,
                           "seconds": round(seconds, 1), "output_tail": result.output[-OUTPUT_TAIL_CHARS:]})
        if result.exit_code != 0:
            failed += 1
            logger.error(f"stage {stage.name} exited {result.exit_code}")
        _save(run)  # progress is visible while later stages run

    if run.status != RunStatus.KILLED:
        run.status = RunStatus.SUCCEEDED if failed == 0 else (
            RunStatus.FAILED if failed == len(stages) else RunStatus.PARTIAL)
    run.summary = summarize(before, snapshot_counts())
    run.finished_at = datetime.now(UTC)
    _save(run)
    if send:
        notify(format_summary(run))
    return run


def default_stages(scrape: bool = True) -> list[Stage]:
    return build_stages(get_settings(), scrape=scrape)
