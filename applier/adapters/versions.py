"""Versioned adapter specs: stage → validate → promote, and one-command rollback.

The built-in spec is version 1. Every later version is a JSON spec file with an
AdapterVersion row recording its parent, the fix, the evidence and its
snapshot pass rate. Exactly one version per domain is active; the applier
always uses the active one. Nothing is deleted, so any version can come back.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlmodel import col, select

from applier.adapters.spec import AdapterSpec
from shared.db.engine import get_session
from shared.paths import adapter_versions_dir
from shared.schemas.adapter import AdapterVersion

BUILTIN = "builtin"


def version_id(domain: str, version: int) -> str:
    return hashlib.sha256(f"{domain}|v{version}".encode()).hexdigest()[:16]


def versions(domain: str) -> list[AdapterVersion]:
    with get_session() as s:
        return list(s.exec(select(AdapterVersion).where(AdapterVersion.domain == domain)
                           .order_by(col(AdapterVersion.version))).all())


def _seed(spec: AdapterSpec) -> AdapterVersion:
    """Register a built-in spec as the domain's first, active version."""
    path = adapter_versions_dir(spec.domain) / f"v{spec.version}.json"
    spec.dump(path)
    row = AdapterVersion(id=version_id(spec.domain, spec.version), domain=spec.domain, version=spec.version,
                         adapter_code_path=str(path), fix_summary="built-in spec", source=BUILTIN,
                         snapshot_pass_rate=0.0, is_active=True)
    with get_session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    return row


def active_version(builtin: AdapterSpec) -> AdapterVersion:
    with get_session() as s:
        row = s.exec(select(AdapterVersion).where(AdapterVersion.domain == builtin.domain,
                                                  col(AdapterVersion.is_active).is_(True))).first()
    if row is not None:
        return row
    known = versions(builtin.domain)
    return known[-1] if known else _seed(builtin)


def active_spec(builtin: AdapterSpec) -> AdapterSpec:
    return AdapterSpec.load(Path(active_version(builtin).adapter_code_path))


def promote(spec: AdapterSpec, *, parent: AdapterVersion, fix_summary: str, evidence: dict[str, Any],
            pass_rate: float, source: str) -> AdapterVersion:
    """Save `spec` as the next version and make it the active one."""
    version = max(v.version for v in versions(spec.domain)) + 1
    spec = spec.model_copy(update={"version": version})
    path = adapter_versions_dir(spec.domain) / f"v{version}.json"
    spec.dump(path)
    row = AdapterVersion(id=version_id(spec.domain, version), domain=spec.domain, version=version,
                         parent_version_id=parent.id, adapter_code_path=str(path), fix_summary=fix_summary,
                         evidence=evidence, snapshot_pass_rate=pass_rate, source=source, is_active=True)
    with get_session() as s:
        for other in s.exec(select(AdapterVersion).where(AdapterVersion.domain == spec.domain)).all():
            other.is_active = False
            s.add(other)
        s.add(row)
        s.commit()
        s.refresh(row)
    return row


def rollback(domain: str, version: int) -> AdapterVersion:
    with get_session() as s:
        rows = list(s.exec(select(AdapterVersion).where(AdapterVersion.domain == domain)).all())
        target = next((r for r in rows if r.version == version), None)
        if target is None:
            raise LookupError(f"{domain} has no version {version} (have {sorted(r.version for r in rows)})")
        for row in rows:
            row.is_active = row.version == version
            s.add(row)
        s.commit()
        s.refresh(target)
    return target
