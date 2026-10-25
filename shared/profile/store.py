"""Load/save the profile with atomic writes and version history."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import select

from shared.db.engine import get_session
from shared.paths import profile_json_path
from shared.schemas.profile import Profile
from shared.schemas.profile_record import ProfileRecord


def load_profile() -> Profile | None:
    path = profile_json_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Profile.model_validate(data)


def save_profile(profile: Profile, *, source: str = "manual", notes: str = "") -> ProfileRecord:
    """Atomically write JSON + append a history row."""
    path = profile_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Atomic file write: write temp, fsync, replace.
    payload = profile.model_dump_json(indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".profile-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    # 2. Append history row.
    with get_session() as s:
        prev = s.exec(
            select(ProfileRecord).order_by(ProfileRecord.version.desc()).limit(1)
        ).first()
        next_version = (prev.version + 1) if prev else 1
        record = ProfileRecord(
            id=str(uuid.uuid4()),
            version=next_version,
            profile_json=profile.model_dump(mode="json"),
            source=source,
            notes=notes,
            created_at=datetime.now(UTC),
        )
        s.add(record)
        s.commit()

    logger.info(f"profile saved: v{next_version} (source={source})")
    return record


def list_history(limit: int = 20) -> list[ProfileRecord]:
    with get_session() as s:
        return s.exec(
            select(ProfileRecord).order_by(ProfileRecord.version.desc()).limit(limit)
        ).all()


def restore_version(version: int) -> Profile:
    with get_session() as s:
        rec = s.exec(
            select(ProfileRecord).where(ProfileRecord.version == version)
        ).first()
    if rec is None:
        raise ValueError(f"version {version} not found")
    profile = Profile.model_validate(rec.profile_json)
    save_profile(profile, source="restore", notes=f"restored from v{version}")
    return profile