"""Orchestrate sources with per-source and per-job failure isolation."""

from __future__ import annotations

from collections.abc import Iterable

import httpx
from loguru import logger

from scraper.config import load_companies
from scraper.sources import SOURCE_REGISTRY, BaseSource
from shared.db.engine import get_session
from shared.db.failures import emit_failure
from shared.db.source_health import record_source_failure, record_source_success
from shared.db.upsert import upsert_job
from shared.schemas.failure import FailureClass


def list_sources() -> list[str]:
    return sorted(SOURCE_REGISTRY)


async def _run_source(
    source: BaseSource,
    companies: list[str],
    *,
    client: httpx.AsyncClient,
    limit: int | None,
) -> None:
    for company in companies:
        logger.info(f"{source.name}: fetching company={company}")
        try:
            raws = await source.fetch(client, company)
        except Exception as exc:
            failure_class = getattr(exc, "failure_class", FailureClass.UNKNOWN)
            logger.error(f"{source.name}/{company}: fetch failed: {exc}")
            emit_failure(
                source_module="scraper",
                phase="fetch",
                error=exc,
                failure_class=failure_class,
                domain=source.domain,
                context={"source": source.name, "company": company},
            )
            record_source_failure(f"{source.name}:{company}", str(exc))
            continue

        if limit is not None:
            raws = raws[:limit]

        processed = 0
        with get_session() as session:
            for raw in raws:
                try:
                    job = source.normalize(raw)
                    upsert_job(session, job)
                    processed += 1
                except Exception as exc:
                    logger.exception(
                        f"{source.name}/{company}: normalize failed for "
                        f"external_id={raw.external_id}"
                    )
                    emit_failure(
                        source_module="scraper",
                        phase="normalize",
                        error=exc,
                        failure_class=FailureClass.SCHEMA_DRIFT,
                        domain=source.domain,
                        context={
                            "source": source.name,
                            "company": company,
                            "external_id": raw.external_id,
                        },
                        session=session,
                    )

        record_source_success(f"{source.name}:{company}")
        logger.info(f"{source.name}/{company}: processed={processed}")


async def run_all_sources(
    source_names: Iterable[str],
    *,
    companies_override: list[str] | None = None,
    limit: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    names = [n for n in source_names if n in SOURCE_REGISTRY]
    if not names:
        logger.warning("no valid sources selected")
        return

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "sachin-agent/0.1 (job scraping)"},
        transport=transport,
    ) as client:
        for name in names:
            source = SOURCE_REGISTRY[name]()
            companies = companies_override or load_companies(name)
            if not companies:
                logger.warning(f"{name}: no companies configured; skipping")
                continue
            await _run_source(source, companies, client=client, limit=limit)