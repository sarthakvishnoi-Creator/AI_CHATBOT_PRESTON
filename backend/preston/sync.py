"""The synchronization engine — source state in, PostgreSQL state out.

Source-neutral by construction: it imports the contract, never an
adapter. Adding a MySQL, REST or HTML adapter later changes nothing here,
which is the property that makes this a foundation rather than an
importer.

The order is fixed and each step exists to protect the next:

    inventory → record → validate → canonicalize → hash → persist
                                  ↘ reject, preserving what is stored
    and only after a *proven complete* inventory: reconcile missing

**Per-document transactions.** Each record commits on its own, so a run
interrupted anywhere leaves N complete documents and zero partial ones.
This is also what makes retry safe: a re-run of the same source converges
without duplicating anything, because identity is ``canonical_uri`` and
``ingest_document`` is idempotent on it.

**Failure never destroys knowledge.** An extraction, validation or
persistence failure increments that document's ``consecutive_failure_count``
and touches nothing else — the stored blocks, chunks and hash remain the
last known good copy. A document that fails forever keeps serving correct
content; it does not decay into an empty row.

**Missing is not deletion.** A document absent from a complete inventory
has its ``gone_count`` incremented and is *counted* as a lifecycle
candidate. It is not archived here: the mass-archival threshold is an
open architectural decision (KB §18 OPEN-11), and inventing one would be
exactly the unreviewed destructive behaviour this design refuses.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from preston.core.db import build_session_factory
from preston.ingestion import IngestionOutcome, ingest_document
from preston.models import Document
from preston.runs import RunCounters, RunMode, ingestion_run
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    Inventory,
    SourceAdapter,
    SourceRecord,
    to_canonical,
)
from preston.validation import validate_source_record


@dataclass(frozen=True, slots=True)
class SyncReport:
    """The outcome of one run, for the caller and for tests."""

    run_id: uuid.UUID
    counters: RunCounters
    missing: tuple[str, ...] = ()


async def _bump_failure(factory: async_sessionmaker[AsyncSession], uri: str) -> None:
    """Record a failure against a document without touching its content.

    A targeted counter update, never a write of extracted data: this is
    the code path taken precisely when the extracted data is not
    trustworthy. A URI with no stored document simply matches no rows.
    """
    async with factory() as session, session.begin():
        await session.execute(
            update(Document)
            .where(Document.canonical_uri == uri)
            .values(consecutive_failure_count=Document.consecutive_failure_count + 1)
        )


async def _persist(
    factory: async_sessionmaker[AsyncSession],
    record: SourceRecord,
    run_id: uuid.UUID,
) -> IngestionOutcome:
    """Ingest one record in its own transaction."""
    document = to_canonical(record, run_id=run_id)
    async with factory() as session, session.begin():
        result = await ingest_document(session, document)
        return result.outcome


async def reconcile_missing(
    session: AsyncSession,
    inventory: CompleteInventory,
    *,
    counters: RunCounters,
) -> tuple[str, ...]:
    """Flag active documents the source no longer lists.

    Accepts :class:`CompleteInventory` and nothing else. That is the
    safety property: an incomplete listing is a different type, so this
    function cannot be called with one — an outage cannot be mistaken for
    a mass deletion by any caller, including a future one.

    Increments ``gone_count`` only. Nothing is archived, deleted, or
    stripped of content.
    """
    result = await session.execute(
        select(Document.canonical_uri).where(Document.status == "active")
    )
    known = set(result.scalars())
    missing = tuple(sorted(known - inventory.identities))

    if missing:
        await session.execute(
            update(Document)
            .where(Document.canonical_uri.in_(missing))
            .values(gone_count=Document.gone_count + 1)
        )
    counters.missing_candidates = len(missing)
    return missing


async def _reconcile(
    factory: async_sessionmaker[AsyncSession],
    inventory: Inventory,
    counters: RunCounters,
) -> tuple[str, ...]:
    """Reconcile when — and only when — it is provably safe to."""
    counters.inventory_size = len(inventory.identities)

    if not isinstance(inventory, CompleteInventory):
        counters.inventory_complete = False
        counters.inventory_reason = inventory.reason
        counters.reconciliation_skipped_reason = "inventory incomplete"
        return ()

    counters.inventory_complete = True

    async with factory() as session, session.begin():
        if not inventory.identities:
            # A complete-but-empty inventory would mark the entire corpus
            # missing in one run. A source that genuinely holds nothing is
            # indistinguishable here from one that answered without error
            # and returned nothing, so this refuses rather than guesses.
            # The proportional case (a large but implausible drop) is
            # OPEN-11 and is deliberately not decided here.
            active = await session.scalar(
                select(Document.canonical_uri)
                .where(Document.status == "active")
                .limit(1)
            )
            if active is not None:
                counters.reconciliation_skipped_reason = (
                    "empty inventory while active documents exist"
                )
                return ()

        missing = await reconcile_missing(session, inventory, counters=counters)
        counters.reconciled = True
        return missing


def _count(counters: RunCounters, outcome: IngestionOutcome) -> None:
    """Attribute one ingest outcome to its counter."""
    match outcome:
        case IngestionOutcome.NEW:
            counters.new += 1
        case IngestionOutcome.UNCHANGED:
            counters.unchanged += 1
        case IngestionOutcome.CHANGED:
            counters.changed += 1
        case IngestionOutcome.REPROCESSED:
            counters.reprocessed += 1
        case IngestionOutcome.RESTORED:
            counters.restored += 1


async def synchronize(
    engine: AsyncEngine,
    adapter: SourceAdapter,
    *,
    mode: RunMode = "sync",
) -> SyncReport:
    """Run one full synchronization against a source.

    Raises :class:`~preston.runs.ConcurrentRunError` if another run holds
    the lock, and re-raises whatever the source raises during inventory —
    in both cases the run row records the outcome and stored knowledge is
    untouched. Individual record failures never abort the run.
    """
    factory = build_session_factory(engine)

    async with ingestion_run(
        engine, mode=mode, extractor_version=adapter.extractor_version
    ) as handle:
        counters = handle.counters

        # Inventory first: a source that cannot even be listed is an
        # outage, and the run fails without touching any document.
        inventory = await adapter.inventory()

        async for item in adapter.records():
            if isinstance(item, ExtractionFailure):
                # The source listed this identity but could not produce it.
                # Its stored content stays exactly as it is.
                counters.extraction_failures += 1
                await _bump_failure(factory, item.canonical_uri)
                continue
            record = item
            failure = validate_source_record(record)
            if failure is not None:
                counters.validation_failures += 1
                await _bump_failure(factory, failure.canonical_uri)
                continue
            try:
                outcome = await _persist(factory, record, handle.run_id)
            # Deliberately broad: the failure model requires an *unexpected*
            # exception on one record to be contained, counted, and survived.
            # Narrowing it would let an unforeseen driver error abandon every
            # remaining record in the run.
            except Exception:  # noqa: BLE001
                counters.persistence_failures += 1
                await _bump_failure(factory, record.canonical_uri)
                continue
            _count(counters, outcome)

        missing = await _reconcile(factory, inventory, counters)

    return SyncReport(run_id=handle.run_id, counters=counters, missing=missing)
