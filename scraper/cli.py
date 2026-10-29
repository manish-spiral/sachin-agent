"""`sachin-agent-scraper` CLI."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from typing import Annotated

import typer
from sqlmodel import col, select

from scraper.normalize import apply_normalization
from scraper.runner import list_sources, run_all_sources, run_dedup
from scraper.sources import SOURCE_REGISTRY
from shared.db.engine import get_session
from shared.logging import configure_logging
from shared.paths import ensure_dirs
from shared.schemas.job import Job
from shared.schemas.source_health import SourceHealth

app = typer.Typer(help="Job lead scraper", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("scraper")
    ensure_dirs()


@app.command()
def sources() -> None:
    """List available sources."""
    for name in list_sources():
        cls = SOURCE_REGISTRY[name]
        notes = []
        if cls.default_boards:
            notes.append(f"default boards: {', '.join(cls.default_boards)}")
        if cls.min_interval_hours:
            notes.append(f"min interval: {cls.min_interval_hours:g}h")
        typer.echo(f"  {name:<16} {'; '.join(notes)}")


@app.command()
def run(
    source: Annotated[
        str,
        typer.Option("--source", "-s", help="Source name (see `sources`) or 'all'."),
    ] = "all",
    companies: Annotated[
        str | None,
        typer.Option(
            "--companies",
            "-c",
            help="Comma-separated boards. Overrides scraper/companies.json.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-l", help="Max jobs per board (debug)."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Ignore per-source minimum intervals."),
    ] = False,
) -> None:
    """Fetch jobs from all sources, upsert, normalize and dedup."""
    valid = set(list_sources())
    if source == "all":
        selected = sorted(valid)
    elif source in valid:
        selected = [source]
    else:
        typer.echo(f"Unknown source: {source}. Valid: all, {', '.join(sorted(valid))}")
        raise typer.Exit(1)

    override = None
    if companies:
        override = [c.strip() for c in companies.split(",") if c.strip()]

    report = asyncio.run(
        run_all_sources(selected, companies_override=override, limit=limit, force=force)
    )
    typer.echo(f"processed {sum(report.processed.values())} jobs from {len(report.processed)} boards")
    if report.skipped_boards:
        typer.echo(f"skipped (min interval): {', '.join(report.skipped_boards)}")
    if report.failed_boards:
        typer.echo(f"failed: {', '.join(report.failed_boards)}")
    if report.dedup:
        typer.echo(f"duplicates: {report.dedup.duplicates} of {report.dedup.jobs} jobs")


@app.command()
def normalize() -> None:
    """Recompute normalized keys for every stored job, then re-run dedup."""
    with get_session() as session:
        jobs = list(session.exec(select(Job)).all())
        for job in jobs:
            session.add(apply_normalization(job))
        session.commit()
    report = run_dedup()
    typer.echo(f"normalized {len(jobs)} jobs; duplicates: {report.duplicates}")


def _fmt(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d %H:%M") if ts else "-"


@app.command()
def health(
    by_source: Annotated[
        bool, typer.Option("--by-source", help="Aggregate boards per source.")
    ] = False,
) -> None:
    """Per-board (or per-source) run success rate, last success, last error."""
    with get_session() as session:
        rows = list(session.exec(select(SourceHealth).order_by(col(SourceHealth.source))).all())
    if not rows:
        typer.echo("No source runs recorded yet.")
        return

    if by_source:
        merged: dict[str, SourceHealth] = {}
        for row in rows:
            name = row.source.split(":", 1)[0]
            agg = merged.setdefault(name, SourceHealth(source=name))
            agg.success_count += row.success_count
            agg.failure_count += row.failure_count
            if row.last_success and (agg.last_success is None or row.last_success > agg.last_success):
                agg.last_success = row.last_success
            if row.last_error and (agg.last_error is None or row.last_error > agg.last_error):
                agg.last_error = row.last_error
                agg.last_error_message = row.last_error_message
        rows = list(merged.values())

    typer.echo(f"{'source':<36} {'ok':>4} {'fail':>4} {'rate':>6}  {'last success UTC':<16}  {'last error UTC':<16}  message")
    for row in rows:
        runs = row.success_count + row.failure_count
        rate = f"{row.success_count / runs:.0%}" if runs else "-"
        message = (row.last_error_message or "")[:60]
        typer.echo(
            f"{row.source:<36} {row.success_count:>4} {row.failure_count:>4} {rate:>6}  "
            f"{_fmt(row.last_success):<16}  {_fmt(row.last_error):<16}  {message}"
        )


@app.command()
def stats(
    top: Annotated[int, typer.Option("--top", help="Rows per breakdown.")] = 15,
) -> None:
    """Job totals, duplicates, and breakdowns by source and location."""
    with get_session() as session:
        jobs = list(session.exec(select(Job)).all())
    if not jobs:
        typer.echo("No jobs yet.")
        return

    unique = [j for j in jobs if j.duplicate_of is None]
    duplicates = len(jobs) - len(unique)
    typer.echo(f"total: {len(jobs)}  unique: {len(unique)}  duplicates: {duplicates} ({duplicates / len(jobs):.1%})")
    missing_location = sum(1 for j in unique if not j.location_norm)
    typer.echo(f"unique jobs without location_norm: {missing_location}")

    typer.echo("\nby source (unique):")
    for name, count in Counter(j.source.split(":", 1)[0] for j in unique).most_common():
        typer.echo(f"  {name:<20} {count:>6}")

    typer.echo(f"\nby location (unique, top {top}):")
    places = Counter(
        place for j in unique for place in (j.location_norm or "unknown").split(" | ")
    )
    for place, count in places.most_common(top):
        typer.echo(f"  {place:<30} {count:>6}")


if __name__ == "__main__":
    app()
