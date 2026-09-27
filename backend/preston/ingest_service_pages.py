"""The service-page ingestion entry point — the Phase 6.5 composition root.

The second batch composition root, built the same way as the first
(:mod:`preston.ingest_blogs`) and deliberately not built on top of it:
neither imports the other, so a change to one family's wiring cannot
disturb another's. Everything below this module already exists — the
settings, both engine builders, the adapters and the synchronization
engine are reused exactly as they are. This module adds no cleaning rule,
no persistence path and no lifecycle semantics. It is wiring::

    Settings
        -> build_async_engine       (PostgreSQL, preston.core.db)
        -> build_source_engine      (controlled MySQL, read-only, B2)
        -> ServicePageAdapter(MANAGEMENT_TRAINING)   -> synchronize
        -> ServicePageAdapter(GRC)                    -> synchronize
        -> ServicePageAdapter(AUDIT_ASSESSMENT)       -> synchronize
        -> ServicePageAdapter(SECURITY_TESTING)       -> synchronize
        -> ServicePageAdapter(PROFESSIONAL_TRAINING)  -> synchronize
        -> FixedPageAdapter(RESOURCE_PROCESS)         -> synchronize
        -> FixedPageAdapter(PRIVACY_POLICY)           -> synchronize
        -> FixedPageAdapter(CORPORATE)                -> synchronize
        -> FixedPageAdapter(STANDALONE_FAQ)           -> synchronize
        -> FixedPageAdapter(OFFICE_LOCATIONS)         -> synchronize
        -> ServiceFaqAdapter                          -> synchronize

**One run, one scope, each independent.** Management Training
(``management_training``), GRC (``grc``), Audit & Assessment
(``audit_assessment``), Security Testing (``security_testing``) and
Professional Training (``professional_training``) all come from MySQL
through the *same* generic adapter with different specs; the fixed-route
pages and collections (``resource_process``, ``privacy_policy``,
``corporate``, ``standalone_faq``, ``office_locations``) come from MySQL
through :mod:`preston.sources.fixed_pages`; the service FAQ
set (``service_faq``) comes from the committed frontend dataset. Each takes
the advisory lock on its own, gets its own run row and its own report,
and reconciles only its own scope, so no run can mark another scope's
documents — or a Blog document — missing. That independence is not
cosmetic: the FAQ source needs no MySQL at all, so a MySQL outage must
not decide whether the FAQ set is synchronized, and one source failing
must not withhold the others.

**Nothing here starts by itself.** Importing this module builds no engine,
opens no connection and starts no run: every construction happens inside
the ``run_*`` functions, and the only unconditional call lives behind the
``__main__`` guard. No module in ``preston.api``, and not ``preston.main``,
imports this one, so starting Preston can never begin an ingestion run.
Invocation is deliberate and explicit, and a scope must be *named* to run
unless it is in :data:`DEFAULT_SCOPES`::

    uv run python -m preston.ingest_service_pages          # the default scopes
    uv run python -m preston.ingest_service_pages grc      # GRC, and only GRC

**No credential ever reaches the terminal.** A failure is reported by
exception *class name* only — the same rule :mod:`preston.runs` applies to
``abort_reason``, and for the same reason: a SQLAlchemy or driver
exception routinely embeds the connection URL, host and password, and this
output goes to an operator's console or a cron mail.
"""

import asyncio
import logging
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import Any, Final

from sqlalchemy.engine import Engine

from preston.core.config import Settings, get_settings
from preston.core.db import build_async_engine
from preston.core.logging import configure_logging
from preston.runs import ConcurrentRunError, RunMode
from preston.sources.contract import SourceAdapter
from preston.sources.fixed_pages import (
    CORPORATE,
    OFFICE_LOCATIONS,
    PRIVACY_POLICY,
    RESOURCE_PROCESS,
    STANDALONE_FAQ,
    FixedPageAdapter,
    FixedPageFamily,
)
from preston.sources.mysql import build_source_engine
from preston.sources.service_faq import ServiceFaqAdapter
from preston.sources.service_page import (
    AUDIT_ASSESSMENT,
    GRC,
    MANAGEMENT_TRAINING,
    PROFESSIONAL_TRAINING,
    SECURITY_TESTING,
    FamilySpec,
    ServicePageAdapter,
)
from preston.sync import SyncReport, synchronize

logger = logging.getLogger(__name__)

#: What every ``run_*_ingestion`` function in this module looks like.
type FamilyRunner = Callable[..., Coroutine[Any, Any, SyncReport]]


def _require_database_url(settings: Settings) -> str:
    """Return the configured PostgreSQL URL, or say which variable is unset.

    Checked before anything is constructed, so a misconfigured environment
    costs no connection and leaves no engine to dispose of. The message
    names the missing variable and never its value.
    """
    if settings.database_url is None:
        raise RuntimeError("No database is configured (PRESTON_DATABASE_URL is unset).")
    return str(settings.database_url)


async def run_management_training_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the Management Training pipeline and run one synchronization.

    The run row, the advisory lock and every document transaction belong
    to :func:`preston.sync.synchronize`; :func:`_run_mysql_family` owns the
    two engines and nothing else.
    """
    return await _run_mysql_family(settings, MANAGEMENT_TRAINING, mode=mode)


async def run_grc_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the GRC pipeline and run one synchronization.

    The same wiring as Management Training with a different
    :class:`~preston.sources.service_page.FamilySpec` — which is the whole
    point of the generic adapter. GRC reconciles only ``source_scope="grc"``
    and therefore cannot touch a Management Training, Service FAQ or Blog
    document.

    **Never invoked automatically.** It is absent from the default scope
    list in :func:`main`, so a bare ``python -m preston.ingest_service_pages``
    does not run GRC; it must be named explicitly.
    """
    return await _run_mysql_family(settings, GRC, mode=mode)


async def run_audit_assessment_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the Audit & Assessment pipeline and run one synchronization.

    The same wiring again, a third ``FamilySpec``. Reconciles only
    ``source_scope="audit_assessment"``.

    **Never invoked automatically** — absent from :data:`DEFAULT_SCOPES`
    for the same reason GRC is: a new family must be named explicitly the
    first time.
    """
    return await _run_mysql_family(settings, AUDIT_ASSESSMENT, mode=mode)


async def run_security_testing_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the Security Testing pipeline and run one synchronization.

    The same wiring again, a fourth ``FamilySpec``. Reconciles only
    ``source_scope="security_testing"``.

    **Never invoked automatically** — absent from :data:`DEFAULT_SCOPES`
    for the same reason GRC and Audit & Assessment are: a new family must
    be named explicitly the first time.
    """
    return await _run_mysql_family(settings, SECURITY_TESTING, mode=mode)


async def run_resource_process_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the Resource/Process fixed-route pages and run one sync.

    Four fixed-route pages under ``/resources/``, reconciled only within
    ``source_scope="resource_process"``. **Never invoked automatically.**
    """
    return await _run_fixed_pages(settings, RESOURCE_PROCESS, mode=mode)


async def run_privacy_policy_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the Privacy Policy page and run one sync.

    One document, reconciled only within ``source_scope="privacy_policy"``.
    **Never invoked automatically.**
    """
    return await _run_fixed_pages(settings, PRIVACY_POLICY, mode=mode)


async def run_corporate_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the About page and run one sync.

    One composed document, reconciled only within ``source_scope="corporate"``.
    **Never invoked automatically.**
    """
    return await _run_fixed_pages(settings, CORPORATE, mode=mode)


async def run_professional_training_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the Professional Training pipeline and run one sync.

    The generic service-page adapter with a fifth ``FamilySpec``: 13 pages,
    13 documents, reconciled only within
    ``source_scope="professional_training"``. **Never invoked automatically.**
    """
    return await _run_mysql_family(settings, PROFESSIONAL_TRAINING, mode=mode)


async def run_standalone_faq_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the standalone FAQ collection and run one sync.

    One document of active FAQ pairs, reconciled only within
    ``source_scope="standalone_faq"``. **Never invoked automatically.**
    """
    return await _run_fixed_pages(settings, STANDALONE_FAQ, mode=mode)


async def run_office_locations_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the office-locations collection and run one sync.

    One document of every office, reconciled only within
    ``source_scope="office_locations"``. **Never invoked automatically.**
    """
    return await _run_fixed_pages(settings, OFFICE_LOCATIONS, mode=mode)


async def _run_mysql_family(
    settings: Settings, spec: FamilySpec, *, mode: RunMode
) -> SyncReport:
    """Run one service-page family through the generic adapter."""
    return await _run_mysql_source(
        settings, lambda source: ServicePageAdapter(source, spec), mode=mode
    )


async def _run_fixed_pages(
    settings: Settings, family: FixedPageFamily, *, mode: RunMode
) -> SyncReport:
    """Run one fixed-route family through its adapter."""
    return await _run_mysql_source(
        settings, lambda source: FixedPageAdapter(source, family), mode=mode
    )


async def _run_mysql_source(
    settings: Settings,
    make_adapter: Callable[[Engine], SourceAdapter],
    *,
    mode: RunMode,
) -> SyncReport:
    """Run one MySQL-backed adapter, then release both engines.

    Shared by every MySQL-backed scope because the wiring genuinely is the
    same: only the adapter differs. Disposal is nested rather than
    sequential — the MySQL engine is released before the PostgreSQL engine,
    and the PostgreSQL engine is released even when building the MySQL
    engine is what failed.
    """
    database_url = _require_database_url(settings)
    if settings.source_mysql_url is None:
        raise RuntimeError(
            "No MySQL source is configured (PRESTON_SOURCE_MYSQL_URL is unset)."
        )

    engine = build_async_engine(database_url)
    try:
        source_engine = build_source_engine(str(settings.source_mysql_url))
        try:
            return await synchronize(engine, make_adapter(source_engine), mode=mode)
        finally:
            source_engine.dispose()
    finally:
        await engine.dispose()


async def run_service_faq_ingestion(
    settings: Settings, *, mode: RunMode = "sync"
) -> SyncReport:
    """Compose the service-FAQ pipeline and run one synchronization.

    No MySQL engine: the FAQ source is the committed dataset inside
    :mod:`preston.sources.service_faq`, which is why this run is
    independent of the controlled source's availability.
    """
    engine = build_async_engine(_require_database_url(settings))
    try:
        return await synchronize(engine, ServiceFaqAdapter(), mode=mode)
    finally:
        await engine.dispose()


def log_summary(scope: str, report: SyncReport) -> None:
    """Log what one run did.

    Counters and a run id only. No document title, URI or block ever
    reaches this output: a run summary is read in places a document is
    not, and the counters are what an operator acts on.
    """
    counters = report.counters
    logger.info(
        "%s ingestion run %s: new=%d unchanged=%d changed=%d reprocessed=%d "
        "restored=%d missing_candidates=%d",
        scope,
        report.run_id,
        counters.new,
        counters.unchanged,
        counters.changed,
        counters.reprocessed,
        counters.restored,
        counters.missing_candidates,
    )
    logger.info(
        "%s failures: extraction=%d validation=%d persistence=%d",
        scope,
        counters.extraction_failures,
        counters.validation_failures,
        counters.persistence_failures,
    )
    logger.info(
        "%s inventory: size=%d complete=%s reconciled=%s",
        scope,
        counters.inventory_size,
        counters.inventory_complete,
        counters.reconciled,
    )
    # Both reasons are fixed phrases chosen by the engine, never a source
    # value or an exception string, so they are safe to print verbatim.
    if counters.inventory_reason is not None:
        logger.warning("%s inventory incomplete: %s", scope, counters.inventory_reason)
    if counters.reconciliation_skipped_reason is not None:
        logger.warning(
            "%s reconciliation skipped: %s",
            scope,
            counters.reconciliation_skipped_reason,
        )


#: Every scope this entry point can run, by name. A scope must be named on
#: the command line to run, except for those in :data:`DEFAULT_SCOPES`.
SCOPES: Final = (
    "management_training",
    "grc",
    "audit_assessment",
    "security_testing",
    "resource_process",
    "privacy_policy",
    "corporate",
    "professional_training",
    "standalone_faq",
    "office_locations",
    "service_faq",
)


def _runners() -> Mapping[str, FamilyRunner]:
    """Return the scope-to-runner mapping, resolved on every call.

    Built here rather than held as a module constant so the mapping reads
    the module's *current* functions. A constant would capture whatever
    was bound at import time, which would silently ignore a replacement —
    exactly what a test's recorder relies on, and what an operator would
    reasonably expect patching to do.
    """
    return {
        "management_training": run_management_training_ingestion,
        "grc": run_grc_ingestion,
        "audit_assessment": run_audit_assessment_ingestion,
        "security_testing": run_security_testing_ingestion,
        "resource_process": run_resource_process_ingestion,
        "privacy_policy": run_privacy_policy_ingestion,
        "corporate": run_corporate_ingestion,
        "professional_training": run_professional_training_ingestion,
        "standalone_faq": run_standalone_faq_ingestion,
        "office_locations": run_office_locations_ingestion,
        "service_faq": run_service_faq_ingestion,
    }


#: What a bare ``python -m preston.ingest_service_pages`` runs — the two
#: scopes that were approved and ingested first, in Phase 6.5. ``grc`` and
#: ``audit_assessment`` are deliberately absent: a new family must be
#: named explicitly the first time, so no one can ingest it by running the
#: module out of habit.
DEFAULT_SCOPES: Final = ("management_training", "service_faq")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the named service-page synchronizations; return one exit code.

    ``0`` everything succeeded, ``2`` nothing failed but at least one run
    did no work because another run held the advisory lock, ``1`` at least
    one run failed or an unknown scope was named. Failure outranks the
    lock, and the lock outranks success, so a scheduler alerting on ``1``
    alone alerts on real failures and not on the outcome the lock exists
    to produce.

    Scopes are positional arguments, run in the order given::

        python -m preston.ingest_service_pages grc

    Each scope runs inside its own boundary: a later one is attempted even
    when an earlier one fails, because they share nothing but a database.
    """
    # ``None`` means "the default scopes", never "read sys.argv": the
    # process argv is passed in explicitly by the ``__main__`` guard, so
    # calling ``main()`` from a test or another process cannot accidentally
    # inherit that process's command line.
    scopes = tuple(argv) if argv else DEFAULT_SCOPES

    runners = _runners()
    unknown = [scope for scope in scopes if scope not in runners]
    if unknown:
        # Printed, not logged: this is a usage error before any logging
        # configuration or database work has happened.
        print(
            f"Unknown scope(s): {', '.join(unknown)}. "
            f"Known scopes: {', '.join(SCOPES)}.",
            file=sys.stderr,
        )
        return 1

    settings = get_settings()
    configure_logging(settings.log_level)

    exit_code = 0
    for scope in scopes:
        run = runners[scope]
        try:
            report = asyncio.run(run(settings))
        except ConcurrentRunError as exc:
            logger.warning("%s: %s", scope, exc.detail)
            exit_code = exit_code or 2
        except Exception as exc:  # noqa: BLE001
            # Class name only — never the message, which may carry the
            # connection URL. ``ingestion_run`` has already closed the run
            # row as ``failed`` with this same class name as its abort
            # reason, so nothing is lost by withholding the text here.
            logger.error("%s ingestion run failed: %s", scope, type(exc).__name__)
            exit_code = 1
        else:
            log_summary(scope, report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
