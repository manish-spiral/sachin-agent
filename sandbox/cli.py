"""`sachin-agent-sandbox` CLI: inspect and replay snapshots offline."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from sqlmodel import col, select

from sandbox.offline import SnapshotNotFound, load_snapshot, offline_page
from shared.config import get_settings
from shared.db.engine import get_session
from shared.logging import configure_logging
from shared.paths import ensure_dirs, snapshot_run_dir
from shared.schemas.snapshot import SnapshotIndex

app = typer.Typer(help="Offline snapshot sandbox", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("sandbox")
    ensure_dirs()


@app.command("list")
def list_snapshots(
    domain: Annotated[str | None, typer.Option("--domain")] = None,
    run: Annotated[str | None, typer.Option("--run")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 30,
) -> None:
    """Recent snapshots."""
    query = select(SnapshotIndex).order_by(col(SnapshotIndex.created_at).desc()).limit(limit)
    if domain:
        query = query.where(SnapshotIndex.domain == domain)
    if run:
        query = query.where(SnapshotIndex.run_id == run)
    with get_session() as s:
        rows = list(s.exec(query).all())
    if not rows:
        typer.echo("No snapshots yet.")
    for row in rows:
        typer.echo(f"{row.id:<52} {row.domain:<24} {row.intent:<22} {row.page_signature}")


@app.command()
def replay(
    snapshot_id: str,
    javascript: Annotated[bool, typer.Option("--js", help="Run the page's scripts (from the HAR).")] = False,
    screenshot: Annotated[Path | None, typer.Option("--screenshot", help="Save what replay renders.")] = None,
) -> None:
    """Load a snapshot offline (no live network) and describe what's on it."""
    try:
        snap = load_snapshot(snapshot_id)
    except SnapshotNotFound:
        typer.echo(f"✗ no snapshot {snapshot_id}", err=True)
        raise typer.Exit(1) from None
    if snap.intent.startswith("api:"):
        data = json.loads(Path(snap.html_path).read_text(encoding="utf-8"))
        typer.echo(f"{snap.url}: {len(data['jobs'])} jobs fetched {data['fetched_at']}  {snap.page_signature}")
        return
    with offline_page(snap, javascript=javascript) as page:
        controls = Counter(page.locator("input, select, textarea").evaluate_all(
            "els => els.map(e => e.tagName.toLowerCase() + ':' + (e.type || ''))"))
        typer.echo(f"{page.title()}\n{snap.url}  (offline, intent={snap.intent})")
        typer.echo(f"signature {snap.page_signature}; controls {dict(controls)}")
        typer.echo(f"buttons: {page.get_by_role('button').all_inner_texts()[:10]}")
        if screenshot is not None:
            page.screenshot(path=str(screenshot), full_page=True)
            typer.echo(f"wrote {screenshot}")


@app.command()
def diagnose(
    llm: Annotated[bool, typer.Option("--llm/--rules-only", help="Use the LLM for what rules can't place.")] = True,
    redo: Annotated[bool, typer.Option("--redo", help="Re-diagnose already diagnosed failures too.")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    """Classify failures into the taxonomy (rules first, then the LLM)."""
    from sandbox.diagnose import diagnose_pending
    from shared.llm import LLMNotConfigured, StructuredLLM, Task, resolve

    model = None
    if llm:
        try:
            resolve(Task.DIAGNOSE)
            model = StructuredLLM()
        except LLMNotConfigured:
            typer.echo("No LLM configured: rules only.")
    report = diagnose_pending(llm=model, limit=limit, redo=redo)
    typer.echo(f"diagnosed by: {dict(report.by_method) or 'nothing'}; classes: {dict(report.by_class)}")
    typer.echo(f"reclassified from the emitter's guess: {report.reclassified}; still undiagnosed: {report.left}")


@app.command()
def prune(days: Annotated[int | None, typer.Option("--days")] = None) -> None:
    """Delete snapshot runs older than the retention window."""
    keep = days if days is not None else get_settings().snapshot_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=keep)
    with get_session() as s:
        old = list(s.exec(select(SnapshotIndex).where(SnapshotIndex.created_at < cutoff)).all())
        runs = {row.run_id for row in old}
        for row in old:
            s.delete(row)
        s.commit()
    for run_id in runs:
        shutil.rmtree(snapshot_run_dir(run_id), ignore_errors=True)
    typer.echo(f"pruned {len(old)} snapshots in {len(runs)} runs older than {keep} days")


if __name__ == "__main__":
    app()
