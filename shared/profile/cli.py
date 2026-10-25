"""`sachin-agent-profile` CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from shared.logging import configure_logging
from shared.paths import ensure_dirs, profile_md_path
from shared.profile.extract import extract_text
from shared.profile.llm import ExtractionError, extract_profile_from_text
from shared.profile.render import render_markdown
from shared.profile.store import (
    list_history,
    load_profile,
    restore_version,
    save_profile,
)
from shared.schemas.profile import Profile

app = typer.Typer(help="Applicant profile management", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("profile")
    ensure_dirs()


@app.command()
def init() -> None:
    """Scaffold an empty profile.json."""
    if load_profile() is not None:
        typer.echo("profile.json already exists. Use `validate` or `render`.")
        raise typer.Exit(1)
    profile = Profile()
    save_profile(profile, source="init", notes="scaffold")
    typer.echo("Created profile-data/profile.json (draft).")
    typer.echo("Next: `sachin-agent-profile import <cv.pdf>` or edit the JSON manually.")


@app.command("import")
def import_cv(path: Annotated[Path, typer.Argument(exists=True, readable=True)],) -> None:
    """Extract a profile from a CV (PDF/DOCX/TXT) using the LLM."""
    try:
        text = extract_text(path)
    except ValueError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e

    try:
        profile = extract_profile_from_text(text)
    except ExtractionError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e

    save_profile(profile, source="import", notes=f"from {path.name}")
    issues = profile.completeness_issues()
    typer.echo(f"Imported draft from {path.name} ({len(text)} chars).")
    if issues:
        typer.echo("Draft is incomplete. Run `validate` to see what's missing:")
        for i in issues:
            typer.echo(f"  - {i}")
    else:
        typer.echo("Draft is complete. Review it, then set `finalized: true` in the JSON.")


@app.command()
def validate() -> None:
    """Check the profile for completeness and schema errors."""
    try:
        profile = load_profile()
    except ValidationError as e:
        typer.echo(f"✗ Schema errors:\n{e}", err=True)
        raise typer.Exit(1) from e

    if profile is None:
        typer.echo("No profile found. Run `init` first.")
        raise typer.Exit(1) 

    issues = profile.completeness_issues()
    if not issues and profile.finalized:
        typer.echo("✅ Profile is valid and finalized.")
        return
    if not issues:
        typer.echo("✅ Profile is complete but not finalized. Set `finalized: true`.")
        return

    typer.echo(f"⚠️  {len(issues)} issue(s):")
    for i in issues:
        typer.echo(f"  - {i}")
    raise typer.Exit(1) 


@app.command()
def render() -> None:
    """Write profile.md from the current JSON."""
    profile = load_profile()
    if profile is None:
        typer.echo("No profile found. Run `init` first.")
        raise typer.Exit(1)
    md = render_markdown(profile)
    out = profile_md_path()
    out.write_text(md, encoding="utf-8")
    typer.echo(f"Wrote {out}")


@app.command()
def history(limit: int = 20) -> None:
    """Show recent profile versions."""
    rows = list_history(limit=limit)
    if not rows:
        typer.echo("No history yet.")
        return
    for r in rows:
        typer.echo(f"  v{r.version:<3} {r.created_at:%Y-%m-%d %H:%M}  {r.source:<8} {r.notes}")


@app.command()
def restore(version: int = typer.Argument(..., min=1)) -> None:
    """Restore a previous version (appends a new history row)."""
    try:
        restore_version(version)
    except ValueError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Restored v{version}. Run `validate` to confirm.")


if __name__ == "__main__":
    app()