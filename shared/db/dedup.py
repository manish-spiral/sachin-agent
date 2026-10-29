"""Mark duplicate jobs across sources by setting Job.duplicate_of.

Operates only on the normalized keys the scraper stored (title_norm,
company_norm, location_norm), so the rules for building those keys stay in the
scraper. A full recompute on every call keeps this idempotent: the same rows
always produce the same clusters and the same canonical job.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlmodel import Session, select

from shared.schemas.job import Job

FUZZY_TITLE_THRESHOLD = 92


@dataclass(frozen=True)
class DedupReport:
    jobs: int
    duplicates: int
    changed: int


def _family(source: str) -> str:
    return source.split(":", 1)[0]


def _locations_compatible(a: str | None, b: str | None) -> bool:
    """Unknown location is compatible with anything; otherwise any shared place."""
    if not a or not b:
        return True
    return bool(set(a.split(" | ")) & set(b.split(" | ")))


def _is_duplicate(a: Job, b: Job) -> bool:
    if not _locations_compatible(a.location_norm, b.location_norm):
        return False
    if _family(a.source) == _family(b.source):
        # One ATS listing the same req twice is rare and usually deliberate
        # (different teams); only identical text counts as a duplicate.
        return (
            a.title_norm == b.title_norm
            and a.location_norm == b.location_norm
            and a.description_raw == b.description_raw
        )
    return fuzz.token_sort_ratio(a.title_norm or "", b.title_norm or "") >= FUZZY_TITLE_THRESHOLD


def dedup_jobs(session: Session, *, priority: dict[str, int] | None = None) -> DedupReport:
    """Cluster duplicates within each company and point them at one canonical job.

    `priority` maps a source family (e.g. "greenhouse") to a rank; lower wins
    canonical. Unlisted families rank last. Ties go to the earliest-seen job.
    """
    ranks = priority or {}
    jobs = list(session.exec(select(Job)).all())

    by_company: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        if job.company_norm and job.title_norm:
            by_company[job.company_norm].append(job)

    canonical_of: dict[str, str] = {}
    for group in by_company.values():
        group.sort(key=lambda j: (ranks.get(_family(j.source), len(ranks)), j.first_seen_at, j.id))
        canonicals: list[Job] = []
        for job in group:
            match = next((c for c in canonicals if _is_duplicate(c, job)), None)
            if match is None:
                canonicals.append(job)
            else:
                canonical_of[job.id] = match.id

    changed = 0
    for job in jobs:
        target = canonical_of.get(job.id)
        if job.duplicate_of != target:
            job.duplicate_of = target
            session.add(job)
            changed += 1
    session.commit()
    return DedupReport(jobs=len(jobs), duplicates=len(canonical_of), changed=changed)
