"""`jobagent-db` CLI."""

from __future__ import annotations

from collections import Counter

import typer
from sqlalchemy import select

from shared.db.engine import get_session
from shared.db.migrate import drop_db
from shared.logging import configure_logging
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

if __name__ == "__main__":
    app()