"""Canonical path helpers. Never hardcode a path elsewhere."""

from __future__ import annotations

from pathlib import Path

from shared.config import get_settings


def ensure_dirs() -> None:
    s = get_settings()
    for d in (
        s.data_dir,
        s.snapshot_dir,
        s.browser_profile_dir,
        s.logs_dir,
        s.profile_data_dir,
    ):
        Path(d).mkdir(parents=True, exist_ok=True)


def profile_json_path() -> Path:
    return Path(get_settings().profile_data_dir) / "profile.json"


def profile_md_path() -> Path:
    return Path(get_settings().profile_data_dir) / "profile.md"


def snapshot_run_dir(run_id: str) -> Path:
    return Path(get_settings().snapshot_dir) / run_id