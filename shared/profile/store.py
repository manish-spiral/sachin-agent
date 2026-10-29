"""Load/save the profile with atomic writes and version history."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import col, select

from shared.db.engine import get_session
from shared.paths import profile_json_path
from shared.profile.facts import active_facts, sync_facts
from shared.schemas.profile import Profile
from shared.schemas.profile_record import ProfileRecord


def load_profile() -> Profile | None:
    path = profile_json_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Profile.model_validate(data)


def save_profile(
    profile: Profile, *, source: str = "manual", notes: str = "", source_text: str | None = None
) -> ProfileRecord:
    """Atomically write JSON, append a history row, and sync profile_facts.

    `source_text` is the CV the profile was extracted from; facts found in it
    get the matching CV line as evidence.
    """
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
            select(ProfileRecord).order_by(col(ProfileRecord.version).desc()).limit(1)
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
        report = sync_facts(s, profile, version=next_version, source_text=source_text)

    logger.info(
        f"profile saved: v{next_version} (source={source}); facts active={report.active} "
        f"added={report.added} updated={report.updated} deactivated={report.deactivated}"
    )
    return record


def list_history(limit: int = 20) -> list[ProfileRecord]:
    with get_session() as s:
        return list(s.exec(
            select(ProfileRecord).order_by(col(ProfileRecord.version).desc()).limit(limit)
        ).all()
    )


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

def load_current_profile() -> tuple[Profile, int] | None:
    """The profile on disk plus the history version that matches it.

    profile.json is meant to be edited by hand (e.g. to set finalized=true). A
    hand edit has no history row and stale facts, so it is recorded here as a
    new version before anything downstream reads it.
    """
    profile = load_profile()
    if profile is None:
        return None
    latest = list_history(limit=1)
    if latest and latest[0].profile_json == profile.model_dump(mode="json"):
        if not active_facts(include_pii=True):
            # Profile saved before profile_facts existed: backfill its facts.
            with get_session() as s:
                sync_facts(s, profile, version=latest[0].version)
        return profile, latest[0].version
    record = save_profile(profile, source="manual", notes="hand edit of profile.json")
    return profile, record.version
