"""`sachin-agent-applier` CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from sqlmodel import col, select

from applier.adapters import adapter_for_url
from applier.context import Answer, ApplicantContext
from applier.human import Absent, Human, Terminal
from applier.memory import forget_consents, remember_answer, remember_consent
from applier.runner import ApplyError, application_url, run_application
from applier.session import BrowserSession
from shared.config import get_settings
from shared.db.engine import get_session
from shared.logging import configure_logging
from shared.paths import ensure_dirs
from shared.profile.facts import active_facts
from shared.schemas.application import Application, ApplicationStatus
from shared.schemas.consent import ConsentKind
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


def _inferrer():
    """LLM answer inference when an LLM is configured; otherwise unknowns go
    straight to the human."""
    from applier.answering import FactInferrer
    from shared.llm import LLMNotConfigured, StructuredLLM, Task, resolve

    try:
        resolve(Task.FORM_ANSWER)
    except LLMNotConfigured:
        return None
    llm = StructuredLLM()
    return lambda facts: FactInferrer(llm, facts)


def _human(interactive: bool) -> Human:
    return Terminal() if interactive else Absent()


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
            human=_human(interactive and headed),
            session_factory=_session(headed),
            inferrer=_inferrer(),
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
    paused = _find_application(application_id)
    try:
        application = run_application(
            paused.job_id,
            human=_human(headed),
            session_factory=_session(headed),
            inferrer=_inferrer(),
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


def _find_application(prefix: str) -> Application:
    with get_session() as s:
        matches = list(s.exec(select(Application).where(col(Application.id).startswith(prefix))).all())
    if len(matches) != 1:
        typer.echo(f"✗ application id {prefix!r} matches {len(matches)}", err=True)
        raise typer.Exit(1)
    return matches[0]


@app.command()
def answer(application_id: Annotated[str, typer.Argument(help="Paused application id or prefix.")]) -> None:
    """Answer the questions a paused application is waiting on. Answers are
    saved to your profile's answers bank, so the next form gets them for free."""
    import json

    from shared.paths import application_dir

    application = _find_application(application_id)
    log = application_dir(application.id) / "steps.jsonl"
    fills = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    last = next((e for e in reversed(fills) if e["step"] in {"filled", "refilled"}), None)
    pending = [f for f in (last or {}).get("fields", []) if f["status"] == "unknown"]
    if not pending:
        typer.echo("No unanswered questions recorded for this application.")
        return
    for field in pending:
        hint = f" [{' / '.join(field['options'])}]" if field.get("options") else ""
        value = typer.prompt(f"{field['label']}{hint}", default="", show_default=False).strip()
        if value:
            remember_answer(field["label"], value)
    typer.echo(f"Saved. Continue with: sachin-agent-applier resume {application.id[:12]}")


consent_app = typer.Typer(help="Your per-domain consent decisions", no_args_is_help=True)
app.add_typer(consent_app, name="consent")


@consent_app.command("list")
def consent_list(domain: Annotated[str | None, typer.Argument()] = None) -> None:
    """Remembered consent decisions."""
    from shared.schemas.consent import ConsentChoice

    with get_session() as s:
        query = select(ConsentChoice)
        if domain:
            query = query.where(ConsentChoice.domain == domain)
        rows = list(s.exec(query).all())
    if not rows:
        typer.echo("No consent decisions yet.")
    for row in rows:
        typer.echo(f"{row.domain:<32} {row.kind.value:<16} {row.choice!r:<24} {row.prompt[:60]}")


@consent_app.command("set")
def consent_set(
    domain: str,
    prompt: Annotated[str, typer.Argument(help="Banner text or question, as shown.")],
    choice: Annotated[str, typer.Argument(help="Exact button / option text.")],
    kind: Annotated[ConsentKind, typer.Option("--kind")] = ConsentKind.ACKNOWLEDGEMENT,
) -> None:
    """Record a decision so the agent replays it on this domain."""
    remember_consent(domain, kind, prompt, choice)
    typer.echo(f"Remembered for {domain}: {choice!r}")


@consent_app.command("forget")
def consent_forget(domain: str) -> None:
    """Drop every decision for a domain (the agent will ask again)."""
    typer.echo(f"Forgot {forget_consents(domain)} decision(s) for {domain}.")


if __name__ == "__main__":
    app()
