"""`sachin-agent-applier` CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from sqlmodel import col, select

from applier.adapters import adapter_for_url
from applier.adapters.base import FillReport
from applier.context import Answer, ApplicantContext
from applier.runner import ApplyError, application_url, run_application
from applier.session import BrowserSession
from shared.config import get_settings
from shared.db.engine import get_session
from shared.logging import configure_logging
from shared.paths import ensure_dirs
from shared.profile.facts import active_facts
from shared.schemas.application import Application, ApplicationStatus
from shared.schemas.job import Job

app = typer.Typer(help="Job applier", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("applier")
    ensure_dirs()


def _find_job(prefix: str) -> Job:
    with get_session() as s:
        jobs = list(s.exec(select(Job).where(col(Job.id).startswith(prefix)).limit(2)).all())
    if len(jobs) != 1:
        typer.echo(f"✗ job id {prefix!r} matches {len(jobs)} jobs", err=True)
        raise typer.Exit(1)
    return jobs[0]


def _human_approver(interactive: bool):
    def approve(_application: Application, report: FillReport) -> bool:
        typer.echo("\nFilled:")
        for f in report.fields:
            typer.echo(f"  {f.status:<8} {f.label[:60]:<60} {f.value[:40]}")
        if not interactive:
            typer.echo("Paused before submit (pause-before-submit is the default).")
            return False
        return typer.confirm("Review the browser window. Submit this application?", default=False)

    return approve


def _session(headed: bool):
    return lambda: BrowserSession(Path(get_settings().browser_profile_dir), headless=not headed)


def _report(application: Application) -> None:
    reason = f" ({application.pause_reason.value})" if application.pause_reason else ""
    typer.echo(f"{application.id[:12]}  {application.status.value}{reason}  last step: {application.last_step}")


@app.command()
def run(
    job_id: Annotated[str, typer.Argument(help="Approved job id or unique prefix.")],
    headed: Annotated[bool, typer.Option("--headed", help="Show the browser.")] = False,
    interactive: Annotated[
        bool, typer.Option("--interactive", help="Ask before submitting (needs --headed).")
    ] = False,
) -> None:
    """Fill the application form for an approved job; pause before submit."""
    job = _find_job(job_id)
    try:
        application = run_application(
            job.id,
            approve=_human_approver(interactive and headed),
            session_factory=_session(headed),
            interactive=interactive and headed,
        )
    except ApplyError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    _report(application)


@app.command()
def resume(
    application_id: Annotated[str, typer.Argument(help="Application id or unique prefix.")],
    headed: Annotated[bool, typer.Option("--headed")] = True,
) -> None:
    """Reopen a paused application with its saved session and continue."""
    with get_session() as s:
        matches = list(s.exec(select(Application).where(col(Application.id).startswith(application_id))).all())
    if len(matches) != 1:
        typer.echo(f"✗ application id {application_id!r} matches {len(matches)}", err=True)
        raise typer.Exit(1)
    try:
        application = run_application(
            matches[0].job_id,
            approve=_human_approver(headed),
            session_factory=_session(headed),
            interactive=headed,
        )
    except ApplyError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    _report(application)


@app.command("list")
def list_applications(
    status: Annotated[ApplicationStatus | None, typer.Option("--status")] = None,
) -> None:
    """Applications, newest first."""
    query = select(Application).order_by(col(Application.updated_at).desc())
    if status is not None:
        query = query.where(Application.status == status)
    with get_session() as s:
        rows = list(s.exec(query).all())
        jobs = {j.id: j for j in s.exec(select(Job).where(col(Job.id).in_([r.job_id for r in rows]))).all()}
    if not rows:
        typer.echo("No applications yet.")
    for row in rows:
        job = jobs.get(row.job_id)
        reason = f"/{row.pause_reason.value}" if row.pause_reason else ""
        typer.echo(f"{row.id[:12]}  {row.status.value + reason:<26} {row.last_step or '-':<28} "
                   f"{job.title[:40] if job else '?'} @ {job.company if job else '?'}")


@app.command()
def inspect(job_id: Annotated[str, typer.Argument(help="Job id or unique prefix.")]) -> None:
    """Read-only dry run on the live form: what would be filled, what needs you.
    Types nothing, uploads nothing, never submits."""
    job = _find_job(job_id)
    url = application_url(job)
    adapter = adapter_for_url(url)
    if adapter is None:
        typer.echo(f"✗ no adapter for {url} (Phase 15 synthesizes one)", err=True)
        raise typer.Exit(1)
    applicant = ApplicantContext(facts=active_facts(include_pii=True))
    with _session(False)() as session:
        adapter.init(session, url)
        if not adapter.detect(session):
            typer.echo(f"✗ {url}: no {adapter.domain} form detected", err=True)
            raise typer.Exit(1)
        info = adapter.extract(session)
    typer.echo(f"{info.title}\n{info.url}\nstandard fields: {[f.intent for f in adapter.spec.fields]}")
    for question in info.questions:
        result = applicant.answer(question)
        shown = f"→ {result.value} ({result.source.value})" if isinstance(result, Answer) else f"✋ {result.value}"
        typer.echo(f"  {question[:80]:<80} {shown}")


if __name__ == "__main__":
    app()
