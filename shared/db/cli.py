"""`jobagent-db` CLI."""

from __future__ import annotations

from collections import Counter

import typer
from sqlalchemy import select

from shared.db.engine import get_session
from shared.db.migrate import drop_db, init_db
from shared.logging import configure_logging
from shared.schemas.job import Job

app = typer.Typer(help="Database management", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("db")


@app.command()
def init() -> None:
    """Create all tables (idempotent)."""
    init_db()
    typer.echo("Database initialized.")


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