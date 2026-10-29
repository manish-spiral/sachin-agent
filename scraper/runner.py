"""Orchestrate sources with per-source and per-job failure isolation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from loguru import logger

from scraper.config import load_companies
from scraper.normalize import apply_normalization
from scraper.sources import SOURCE_REGISTRY, BaseSource
from shared.db.dedup import DedupReport, dedup_jobs
from shared.db.engine import get_session
from shared.db.failures import emit_failure
from shared.db.source_health import record_source_failure, record_source_success
from shared.db.upsert import upsert_job
from shared.schemas.failure import FailureClass
from shared.schemas.source_health import SourceHealth


@dataclass
class RunReport:
    processed: dict[str, int] = field(default_factory=dict)
    failed_boards: list[str] = field(default_factory=list)
    skipped_boards: list[str] = field(default_factory=list)
    dedup: DedupReport | None = None


def list_sources() -> list[str]:
    return sorted(SOURCE_REGISTRY)


def dedup_priority() -> dict[str, int]:
    return {name: cls.dedup_rank for name, cls in SOURCE_REGISTRY.items()}


def run_dedup() -> DedupReport:
    with get_session() as session:
        return dedup_jobs(session, priority=dedup_priority())


def _too_soon(source: BaseSource, key: str) -> bool:
    if source.min_interval_hours is None:
        return False
    with get_session() as session:
        health = session.get(SourceHealth, key)
    if health is None or health.last_success is None:
        return False
    # SQLite drops tzinfo on the way back; values are always written in UTC.
    last = health.last_success.replace(tzinfo=health.last_success.tzinfo or UTC)
    return datetime.now(UTC) - last < timedelta(hours=source.min_interval_hours)


async def _run_board(
    source: BaseSource,
    board: str,
    *,
    client: httpx.AsyncClient,
    limit: int | None,
    report: RunReport,
) -> None:
    key = f"{source.name}:{board}"
    logger.info(f"{key}: fetching")
    try:
        raws = await source.fetch(client, board)
        if limit is not None:
            raws = raws[:limit]
        raws, enrich_errors = await source.enrich(client, raws)
    except Exception as exc:
        failure_class = getattr(exc, "failure_class", FailureClass.UNKNOWN)
        logger.error(f"{key}: fetch failed: {exc}")
        emit_failure(
            source_module="scraper",
            phase="fetch",
            error=exc,
            failure_class=failure_class,
            domain=source.domain,
            context={"source": source.name, "company": board},
        )
        record_source_failure(key, str(exc))
        report.failed_boards.append(key)
        return

    for raw, error in enrich_errors:
        logger.warning(f"{key}: enrich failed for external_id={raw.external_id}: {error}")
        emit_failure(
            source_module="scraper",
            phase="enrich",
            error=error,
            failure_class=getattr(error, "failure_class", FailureClass.UNKNOWN),
            domain=source.domain,
            context={"source": source.name, "company": board, "external_id": raw.external_id},
        )

    processed = 0
    with get_session() as session:
        for raw in raws:
            try:
                upsert_job(session, apply_normalization(source.normalize(raw)))
                processed += 1
            except Exception as exc:
                session.rollback()
                logger.exception(f"{key}: normalize failed for external_id={raw.external_id}")
                emit_failure(
                    source_module="scraper",
                    phase="normalize",
                    error=exc,
                    failure_class=FailureClass.SCHEMA_DRIFT,
                    domain=source.domain,
                    context={
                        "source": source.name,
                        "company": board,
                        "external_id": raw.external_id,
                    },
                    session=session,
                )

    record_source_success(key)
    report.processed[key] = processed
    logger.info(f"{key}: processed={processed}")


async def run_all_sources(
    source_names: Iterable[str],
    *,
    companies_override: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunReport:
    report = RunReport()
    names = [n for n in source_names if n in SOURCE_REGISTRY]
    if not names:
        logger.warning("no valid sources selected")
        return report

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "sachin-agent/0.1 (job scraping)"},
        transport=transport,
    ) as client:
        for name in names:
            source = SOURCE_REGISTRY[name]()
            boards = companies_override or load_companies(name) or list(source.default_boards)
            if not boards:
                logger.warning(f"{name}: no companies configured; skipping")
                continue
            for board in boards:
                key = f"{name}:{board}"
                if not force and _too_soon(source, key):
                    logger.info(
                        f"{key}: last success < {source.min_interval_hours}h ago "
                        "(source ToS); skipping. Use --force to override."
                    )
                    report.skipped_boards.append(key)
                    continue
                await _run_board(source, board, client=client, limit=limit, report=report)

    report.dedup = run_dedup()
    logger.info(
        f"dedup: {report.dedup.duplicates} duplicates across {report.dedup.jobs} jobs "
        f"({report.dedup.changed} changed)"
    )
    return report
