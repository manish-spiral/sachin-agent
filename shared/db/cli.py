"""`sachin-agent-db` CLI."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import typer
from sqlalchemy import select
from sqlmodel import col

from shared.db.engine import get_session
from shared.db.migrate import drop_db
from shared.logging import configure_logging
from shared.schemas.failure import FailureEvent
from shared.schemas.job import Job

app = typer.Typer(help="Database management", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("db")


def _alembic_config():
    from alembic.config import Config
    return Config("alembic.ini")


@app.command()
def init() -> None:
    """Apply all migrations (idempotent; creates the DB if needed)."""
    from alembic import command
    command.upgrade(_alembic_config(), "head")
    typer.echo("Database initialized.")


@app.command()
def migrate(
    message: str = typer.Option(..., "-m", "--message", help="Migration message"),
) -> None:
    """Autogenerate a new migration from model changes."""
    from alembic import command
    command.revision(_alembic_config(), message=message, autogenerate=True)
    typer.echo("Migration generated. Review it in migrations/versions/ before upgrading.")


@app.command()
def upgrade(revision: str = typer.Argument("head")) -> None:
    """Apply migrations up to REVISION (default: head)."""
    from alembic import command
    command.upgrade(_alembic_config(), revision)
    typer.echo(f"Upgraded to {revision}.")


@app.command()
def downgrade(revision: str = typer.Argument("-1")) -> None:
    """Roll back to REVISION (default: one step)."""
    from alembic import command
    command.downgrade(_alembic_config(), revision)
    typer.echo(f"Downgraded to {revision}.")


@app.command()
def current() -> None:
    """Show current migration revision."""
    from alembic import command
    command.current(_alembic_config())


@app.command()
def stamp(revision: str = typer.Argument("head")) -> None:
    """Mark the DB as being at REVISION without running migrations."""
    from alembic import command
    command.stamp(_alembic_config(), revision)
    typer.echo(f"Stamped at {revision}.")


@app.command()
def drop() -> None:
    """Drop all tables (SQLite only). Requires confirmation."""
    if not typer.confirm("Drop ALL tables? This cannot be undone."):
        raise typer.Abort()
    drop_db()
    typer.echo("Dropped.")


@app.command()
def stats() -> None:
    """Show job counts per status."""
    with get_session() as s:
        jobs = s.scalars(select(Job)).all()
    if not jobs:
        typer.echo("No jobs yet.")
        return
    counts = Counter(j.status for j in jobs)
    for status in sorted(counts, key=lambda st: st.value):
        typer.echo(f"  {status.value}: {counts[status]}")

failures_app = typer.Typer(help="Failure events", no_args_is_help=True)
app.add_typer(failures_app, name="failures")


def _failures(days: int | None) -> list[FailureEvent]:
    query = select(FailureEvent)
    if days is not None:
        query = query.where(col(FailureEvent.created_at) >= datetime.now(UTC) - timedelta(days=days))
    with get_session() as s:
        return list(s.scalars(query).all())


@failures_app.command("stats")
def failures_stats(days: int | None = typer.Option(None, "--days", help="Only the last N days.")) -> None:
    """Failure counts by class, domain and module. Undiagnosed classes are
    the emitter's guess, marked with '?' (run `sachin-agent-sandbox diagnose`)."""
    events = _failures(days)
    if not events:
        typer.echo("No failures recorded.")
        return
    undiagnosed = sum(1 for e in events if e.diagnosed_at is None)
    typer.echo(f"failures: {len(events)}  (undiagnosed: {undiagnosed})")

    def show(title: str, counter: Counter[str]) -> None:
        typer.echo(f"\nby {title}:")
        for key, count in counter.most_common():
            typer.echo(f"  {key:<34} {count:>5}  {count / len(events):>5.0%}")

    show("class", Counter(
        (e.failure_class.value if e.failure_class else "unclassified") + ("" if e.diagnosed_at else "?")
        for e in events))
    show("domain", Counter(e.domain or "-" for e in events))
    show("module/phase", Counter(f"{e.source_module}/{e.phase}" for e in events))


@failures_app.command("list")
def failures_list(
    days: int | None = typer.Option(None, "--days"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Most recent failures with their diagnosis."""
    events = sorted(_failures(days), key=lambda e: e.created_at, reverse=True)[:limit]
    for e in events:
        cls = (e.failure_class.value if e.failure_class else "-") + ("" if e.diagnosed_at else "?")
        conf = (e.diagnosis or {}).get("confidence")  # NULL on rows older than the column
        typer.echo(f"{e.created_at:%Y-%m-%d %H:%M}  {cls:<18} {f'{conf:.2f}' if conf else '    '}  "
                   f"{e.source_module}/{e.phase:<20} {e.error_message[:70]}")


if __name__ == "__main__":
    app()