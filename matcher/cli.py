"""`sachin-agent-matcher` CLI."""

from __future__ import annotations

from collections import Counter
from typing import Annotated

import typer
from sqlmodel import col, select

from matcher.calibrate import (
    evaluate,
    fit_weights,
    label_from_job,
    load_labels,
    sample_for_labeling,
    save_labels,
    save_weights,
)
from matcher.review import (
    JobNotFound,
    approved_without_documents,
    auto_approve,
    decide,
    find_job,
    format_job,
    review_queue,
)
from matcher.scoring import ProfileNotReady, load_weights, score_pending_jobs
from shared.db.engine import get_session
from shared.llm import LLMNotConfigured
from shared.logging import configure_logging
from shared.paths import ensure_dirs
from shared.schemas.job import Job, JobStatus
from shared.schemas.job_transitions import IllegalTransition
from shared.schemas.score import Dimension

app = typer.Typer(help="Job matcher", no_args_is_help=True)
calibrate_app = typer.Typer(help="Compare LLM scores with your judgment", no_args_is_help=True)
app.add_typer(calibrate_app, name="calibrate")


@app.callback()
def _bootstrap() -> None:
    configure_logging("matcher")
    ensure_dirs()


@app.command()
def run(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-l", help="Max LLM calls this run (prefiltered jobs are free)."),
    ] = 100,
    rescore: Annotated[
        bool, typer.Option("--rescore", help="Also re-score already SCORED jobs.")
    ] = False,
) -> None:
    """Score new jobs against the finalized profile."""
    try:
        report = score_pending_jobs(limit=limit, rescore=rescore)
    except (ProfileNotReady, LLMNotConfigured) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"scored: {report.scored}  prefiltered: {report.prefiltered}  failed: {report.failed}  "
        f"citation issues: {report.citation_issues}"
    )
    if report.stopped:
        typer.echo(f"stopped early ({report.stopped}). Unscored jobs stay queued for the next run.")


def _set_status(id_prefix: str, to: JobStatus) -> None:
    try:
        job = decide(find_job(id_prefix).id, to)
    except (JobNotFound, IllegalTransition) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{to.value}: {job.title} @ {job.company}")


@app.command()
def approve(job_id: Annotated[str, typer.Argument(help="Job id or unique prefix.")]) -> None:
    """Approve a scored job for applying."""
    _set_status(job_id, JobStatus.APPROVED)


@app.command()
def reject(job_id: Annotated[str, typer.Argument(help="Job id or unique prefix.")]) -> None:
    """Reject a scored job."""
    _set_status(job_id, JobStatus.REJECTED)


@app.command()
def review(
    top: Annotated[int, typer.Option("--top", "-n", help="Jobs to show.")] = 20,
    min_score: Annotated[float | None, typer.Option("--min-score")] = None,
    interactive: Annotated[
        bool, typer.Option("--interactive/--list", help="Prompt for a decision per job.")
    ] = True,
) -> None:
    """Show the top-scored jobs with reasons; approve or reject each."""
    jobs = review_queue(limit=top, min_score=min_score)
    if not jobs:
        typer.echo("Nothing to review. Run `sachin-agent-matcher run` first.")
        return

    decisions: Counter[str] = Counter()
    for job in jobs:
        typer.echo("\n" + format_job(job))
        if not interactive:
            continue
        choice = typer.prompt("[a]pprove / [r]eject / [s]kip / [q]uit", default="s").strip().lower()
        if choice.startswith("q"):
            break
        if choice.startswith("a"):
            decide(job.id, JobStatus.APPROVED)
            decisions["approved"] += 1
        elif choice.startswith("r"):
            decide(job.id, JobStatus.REJECTED)
            decisions["rejected"] += 1
    if interactive:
        typer.echo(f"\napproved: {decisions['approved']}  rejected: {decisions['rejected']}")


@app.command()
def stats() -> None:
    """Counts by status, score distribution, and the weakest rubric dimensions."""
    with get_session() as session:
        jobs = list(session.exec(select(Job).where(col(Job.duplicate_of).is_(None))).all())
    if not jobs:
        typer.echo("No jobs yet.")
        return

    typer.echo("by status (unique jobs):")
    for status, count in sorted(Counter(j.status.value for j in jobs).items()):
        typer.echo(f"  {status:<10} {count:>6}")

    scored = [j for j in jobs if j.score is not None]
    if not scored:
        return
    methods = Counter((j.score_breakdown or {}).get("method", "?") for j in scored)
    typer.echo(f"\nscored by: {', '.join(f'{m}={c}' for m, c in methods.items())}")
    typer.echo("score distribution:")
    buckets = Counter(min(int(j.score or 0), 4) for j in scored)
    for low in range(1, 5):
        label = f"{low}–{low + 1}"
        typer.echo(f"  {label:<5} {'#' * min(buckets[low], 60)} {buckets[low]}")

    llm = [b for j in scored if (b := (j.score_breakdown or {}).get("breakdown"))]
    if llm:
        typer.echo("\nmean dimension score (LLM-scored jobs):")
        for dim in Dimension:
            mean = sum(b[dim.value]["score"] for b in llm) / len(llm)
            typer.echo(f"  {dim.value:<15} {mean:.2f}")


@app.command()
def tailor(
    job_id: Annotated[str | None, typer.Argument(help="Job id or unique prefix.")] = None,
    top: Annotated[
        int | None, typer.Option("--top", help="Tailor the N best-scored jobs instead.")
    ] = None,
    approved: Annotated[
        int | None, typer.Option("--approved", help="Tailor up to N approved jobs lacking documents.")
    ] = None,
    cover_letter: Annotated[bool, typer.Option("--cover-letter/--no-cover-letter")] = True,
) -> None:
    """Write a verified, ATS-safe resume (and cover letter) PDF for a job."""
    from matcher.llm import StructuredLLM
    from matcher.tailor import TailorError, tailor_job

    if sum(x is not None for x in (job_id, top, approved)) != 1:
        typer.echo("✗ Give exactly one of JOB_ID, --top N or --approved N.", err=True)
        raise typer.Exit(2)
    try:
        if job_id:
            ids = [find_job(job_id).id]
        elif approved is not None:
            ids = [j.id for j in approved_without_documents(approved)]
        else:
            ids = [j.id for j in review_queue(limit=top or 0)]
        llm = StructuredLLM()
    except (JobNotFound, LLMNotConfigured) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc

    from shared.killswitch import is_killed

    for jid in ids:
        if is_killed():
            typer.echo("Kill switch on; stopping.")
            break
        try:
            report = tailor_job(jid, llm=llm, cover_letter=cover_letter)
        except TailorError as exc:
            typer.echo(f"✗ {exc}", err=True)
            raise typer.Exit(1) from exc
        for doc in filter(None, (report.resume, report.cover_letter)):
            how = "profile-only fallback" if doc.fallback else f"tailored, {doc.attempts} attempt(s)"
            rejected = f", {len(doc.violations)} violation(s) caught in rejected drafts" if doc.violations else ""
            typer.echo(f"{doc.kind.value:<13} {doc.pdf_path}  ({how}{rejected})")
        status = "ATS check passed" if not report.ats_problems else f"ATS problems: {report.ats_problems}"
        typer.echo(f"  {status}")


@app.command("auto-approve")
def auto_approve_cmd(
    min_score: Annotated[float, typer.Option("--min-score")],
    limit: Annotated[int, typer.Option("--limit")] = 10,
    exclude_company: Annotated[list[str] | None, typer.Option("--exclude-company")] = None,
    exclude_title: Annotated[list[str] | None, typer.Option("--exclude-title")] = None,
) -> None:
    """Approve the best scored jobs at or above a threshold (policy approval)."""
    approved = auto_approve(min_score, limit=limit, exclude_companies=exclude_company or [],
                            exclude_title_keywords=exclude_title or [])
    for job in approved:
        typer.echo(f"approved {job.score:.2f}  {job.title} @ {job.company}")
    typer.echo(f"auto-approved: {len(approved)}")


@calibrate_app.command("label")
def calibrate_label(
    n: Annotated[int, typer.Option("--n", help="Jobs to label this session.")] = 20,
) -> None:
    """Score jobs yourself, without seeing the LLM's score (so it can't anchor you)."""
    labels = load_labels()
    jobs = sample_for_labeling(n, exclude={label.job_id for label in labels})
    if not jobs:
        typer.echo("No unlabeled LLM-scored jobs. Run `sachin-agent-matcher run` first.")
        return
    typer.echo("Rate each job 1 (no way) – 5 (apply today). [s]kip, [q]uit.")
    for i, job in enumerate(jobs, start=1):
        typer.echo(f"\n({i}/{len(jobs)}) " + format_job(job, show_score=False))
        typer.echo(f"  {job.description_raw[:600]}…")
        answer = typer.prompt("your score", default="s").strip().lower()
        if answer.startswith("q"):
            break
        if answer in {"1", "2", "3", "4", "5"}:
            labels.append(label_from_job(job, int(answer)))
            save_labels(labels)
    typer.echo(f"\n{len(labels)} labels saved. Next: `sachin-agent-matcher calibrate report`.")


@calibrate_app.command("report")
def calibrate_report(
    apply: Annotated[
        bool, typer.Option("--apply", help="Save fitted weights if they agree better.")
    ] = False,
) -> None:
    """Agreement between LLM and manual scores, plus suggested weights."""
    labels = load_labels()
    if not labels:
        typer.echo("No labels yet. Run `sachin-agent-matcher calibrate label`.")
        raise typer.Exit(1)

    current = load_weights()
    fitted = fit_weights(labels, current)
    before, after = evaluate(labels, current), evaluate(labels, fitted)

    typer.echo(f"labels: {before.n}  (agreement = overall within ±1 of your score)")
    typer.echo(f"{'dimension':<15} {'current':>8} {'fitted':>8}")
    for dim in Dimension:
        typer.echo(f"{dim.value:<15} {current[dim.value]:>8.3f} {fitted[dim.value]:>8.3f}")
    typer.echo(f"agreement       {before.agreement:>8.0%} {after.agreement:>8.0%}")
    typer.echo(f"mean abs error  {before.mean_abs_error:>8.2f} {after.mean_abs_error:>8.2f}")

    if not apply:
        return
    if after.mean_abs_error < before.mean_abs_error:
        save_weights(fitted)
        typer.echo("Saved fitted weights. Re-score with `sachin-agent-matcher run --rescore`.")
    else:
        typer.echo("Fitted weights are no better; kept current weights.")


if __name__ == "__main__":
    app()
