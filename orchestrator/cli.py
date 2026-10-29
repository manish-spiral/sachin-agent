"""`sachin-agent-run` CLI: the whole pipeline, its kill switch and its history."""

from __future__ import annotations

from typing import Annotated

import typer
from sqlmodel import col, select

from orchestrator.pipeline import default_stages, format_summary, run_pipeline
from shared.db.engine import get_session
from shared.killswitch import is_killed, kill_file
from shared.logging import configure_logging
from shared.paths import ensure_dirs
from shared.schemas.pipeline_run import PipelineRun, RunStatus

app = typer.Typer(help="Pipeline orchestration", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("orchestrator")
    ensure_dirs()


@app.command("run-all")
def run_all(
    scrape: Annotated[bool, typer.Option("--scrape/--no-scrape")] = True,
    trigger: Annotated[str, typer.Option("--trigger", help="manual | timer")] = "manual",
    notify: Annotated[bool, typer.Option("--notify/--no-notify")] = True,
) -> None:
    """scrape → score → (auto-approve) → tailor → apply, then send a summary."""
    if is_killed():
        typer.echo(f"✗ Kill switch is on ({kill_file()} or KILL_SWITCH). `sachin-agent-run resume` to clear.")
        raise typer.Exit(1)
    run = run_pipeline(default_stages(scrape), trigger=trigger, send=notify)
    typer.echo(format_summary(run))
    if run.status in {RunStatus.FAILED, RunStatus.KILLED}:
        raise typer.Exit(1)


@app.command()
def kill() -> None:
    """Stop everything: running stages finish their current unit, save state and exit."""
    path = kill_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("killed\n")
    typer.echo(f"Kill switch on ({path}).")


@app.command("resume")
def resume_runs() -> None:
    """Clear the kill-switch file."""
    kill_file().unlink(missing_ok=True)
    typer.echo("Kill switch off." if not is_killed() else "File removed, but KILL_SWITCH is still set.")


@app.command()
def history(limit: Annotated[int, typer.Option("--limit")] = 10) -> None:
    """Recent pipeline runs."""
    with get_session() as s:
        runs = list(s.exec(select(PipelineRun).order_by(col(PipelineRun.started_at).desc()).limit(limit)).all())
    if not runs:
        typer.echo("No runs yet.")
    for run in runs:
        stages = " ".join(f"{st['name']}:{st['exit_code']}" for st in run.stages)
        typer.echo(f"{run.started_at:%Y-%m-%d %H:%M}  {run.id[:8]}  {run.status.value:<9} {run.trigger:<6} {stages}")


if __name__ == "__main__":
    app()
