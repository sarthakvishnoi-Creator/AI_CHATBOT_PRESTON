"""The Blog ingestion entry point — the composition root for one run.

``main.py`` is the composition root for the web application and knows
nothing about ingestion. This is the composition root for the batch job,
and the two never import each other. Everything below it already exists:
the settings, both engine builders, the adapter and the synchronization
engine are reused exactly as they are, and this module adds no new
cleaning rule, no new persistence path and no new lifecycle semantics. It
is wiring, and only wiring::

    Settings
        -> build_async_engine       (PostgreSQL, preston.core.db)
        -> build_source_engine      (controlled MySQL, read-only, B2)
        -> BlogAdapter              (preston.sources.blog)
        -> synchronize              (preston.sync — runs, locks, persists)
        -> SyncReport               (logged as a counter summary)

**Nothing here starts by itself.** Importing this module builds no
engine, opens no connection and starts no run: every construction happens
inside :func:`run_blog_ingestion`, and the only unconditional call to it
lives behind the ``__main__`` guard at the bottom of this file. No module
in ``preston.api``, and not ``preston.main``, imports this one — so
starting Preston, or importing any part of it, can never begin an
ingestion run. Invocation is deliberate and explicit::

    uv run python -m preston.ingest_blogs

**Read-only at the source is preserved, not re-implemented.** The engine
comes from :func:`preston.sources.mysql.build_source_engine`, which
registers ``SET SESSION TRANSACTION READ ONLY`` on every connection it
opens; this module never builds a MySQL engine any other way and issues
no statement of its own.

**No credential ever reaches the terminal.** A failure is reported by
exception *class name* only, the same rule
:mod:`preston.runs` applies to ``abort_reason`` and for the same reason:
a SQLAlchemy or driver exception routinely embeds the connection URL,
host and password, and this output goes to an operator's console or a
cron mail.
"""

import asyncio
import logging

from preston.core.config import Settings, get_settings
from preston.core.db import build_async_engine
from preston.core.logging import configure_logging
from preston.runs import ConcurrentRunError, RunMode
from preston.sources.blog import BlogAdapter
from preston.sources.mysql import build_source_engine
from preston.sync import SyncReport, synchronize

logger = logging.getLogger(__name__)


async def run_blog_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the Blog pipeline and run exactly one synchronization.

    Both URLs are checked before anything is constructed, so a
    misconfigured environment costs no connection and leaves no engine to
    dispose of. The messages name the missing variable and never its
    value.

    Disposal is nested rather than sequential: the MySQL engine is
    released before the PostgreSQL engine, and the PostgreSQL engine is
    released even when building the MySQL engine is what failed. The run
    row, the advisory lock and every document transaction belong to
    :func:`preston.sync.synchronize`; this function owns the two engines
    and nothing else.
    """
    if settings.database_url is None:
        raise RuntimeError("No database is configured (PRESTON_DATABASE_URL is unset).")
    if settings.source_mysql_url is None:
        raise RuntimeError(
            "No MySQL source is configured (PRESTON_SOURCE_MYSQL_URL is unset)."
        )

    engine = build_async_engine(str(settings.database_url))
    try:
        source_engine = build_source_engine(str(settings.source_mysql_url))
        try:
            return await synchronize(engine, BlogAdapter(source_engine), mode=mode)
        finally:
            source_engine.dispose()
    finally:
        await engine.dispose()


def log_summary(report: SyncReport) -> None:
    """Log what the run did.

    Counters and a run id only. No document title, URI or block ever
    reaches this output: a run summary is read in places a document is
    not, and the counters are what an operator acts on.
    """
    counters = report.counters
    logger.info(
        "Blog ingestion run %s: new=%d unchanged=%d changed=%d reprocessed=%d "
        "restored=%d missing_candidates=%d",
        report.run_id,
        counters.new,
        counters.unchanged,
        counters.changed,
        counters.reprocessed,
        counters.restored,
        counters.missing_candidates,
    )
    logger.info(
        "Failures: extraction=%d validation=%d persistence=%d",
        counters.extraction_failures,
        counters.validation_failures,
        counters.persistence_failures,
    )
    logger.info(
        "Inventory: size=%d complete=%s reconciled=%s",
        counters.inventory_size,
        counters.inventory_complete,
        counters.reconciled,
    )
    # Both reasons are fixed phrases chosen by the engine, never a source
    # value or an exception string, so they are safe to print verbatim.
    if counters.inventory_reason is not None:
        logger.warning("Inventory incomplete: %s", counters.inventory_reason)
    if counters.reconciliation_skipped_reason is not None:
        logger.warning(
            "Reconciliation skipped: %s", counters.reconciliation_skipped_reason
        )


def main() -> int:
    """Run one Blog ingestion and return a process exit code.

    ``0`` the run succeeded, ``1`` it failed, ``2`` another run holds the
    advisory lock and this process did no work at all. The last is a
    distinct code because it is not an error: it is the outcome the lock
    exists to produce, and a scheduler should not alert on it.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        report = asyncio.run(run_blog_ingestion(settings))
    except ConcurrentRunError as exc:
        logger.warning("%s", exc.detail)
        return 2
    except Exception as exc:  # noqa: BLE001
        # Class name only — never the message, which may carry the
        # connection URL. `ingestion_run` has already closed the run row
        # as ``failed`` with this same class name as its abort reason, so
        # nothing is lost by withholding the text here.
        logger.error("Blog ingestion run failed: %s", type(exc).__name__)
        return 1
    log_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
