"""Ingestion run lifecycle — what happened, and whether it can be trusted.

Owns the ``ingestion_runs`` row for one execution: opening it, counting
what the run did, and closing it with a status that cannot be mistaken
for something else. Until this module existed the table was schema with
no writer; nothing else in the codebase creates a run row.

**A failed run must never read as a successful one.** The row is inserted
as ``running`` and committed *before* any work starts, so a process that
dies leaves visible evidence rather than nothing at all. The next run
closes such a row as ``aborted`` — and can do so safely without a
staleness timeout, because it only sweeps while holding the exclusive
advisory lock, which means no legitimately-running run exists.

**Concurrency is a PostgreSQL advisory lock** (architecture §15): one
session-level lock, held on a dedicated connection for the run's
duration, taken with ``pg_try_advisory_lock`` so a second run exits
cleanly instead of blocking or corrupting counts. Nothing else is
introduced — no queue, no broker, no lock service.

**Nothing here stores a credential.** ``abort_reason`` records an
exception's *class name* only. A SQLAlchemy or driver exception's message
routinely carries the connection URL, host and user, and a run row is a
long-lived, widely-read record.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from preston.canonical import HASH_VERSION
from preston.core.db import build_session_factory
from preston.core.errors import PrestonError
from preston.ingestion import CHUNKER_VERSION
from preston.models import IngestionRun
from preston.normalization import NORMALIZER_VERSION

RunMode = Literal["sync", "reprocess", "backfill"]

# One fixed key for the whole ingestion pipeline. Session-level advisory
# locks are namespaced by this integer alone, so it must never collide
# with another use; it is a constant rather than derived so that reading
# two processes' code is enough to prove they contend for the same lock.
RUN_ADVISORY_LOCK_KEY: Final = 6_300_100_000_000_001


class ConcurrentRunError(PrestonError):
    """Another ingestion run holds the lock.

    Not a failure of this run's work — no work was done. The caller is
    expected to exit cleanly; a second concurrent run producing half a
    corpus is the outcome the lock exists to prevent.
    """

    title = "Conflict"
    status_code = 409


@dataclass(slots=True)
class RunCounters:
    """What the run did, in the shape ``ingestion_runs.counts`` stores.

    Outcome counters mirror :class:`~preston.ingestion.IngestionOutcome`
    exactly. Failure counters are kept apart by *stage* because they mean
    different things operationally: extraction failures point at the
    source, validation failures at the extractor's output, persistence
    failures at this system.
    """

    new: int = 0
    unchanged: int = 0
    changed: int = 0
    reprocessed: int = 0
    restored: int = 0
    missing_candidates: int = 0
    extraction_failures: int = 0
    validation_failures: int = 0
    persistence_failures: int = 0
    inventory_complete: bool = False
    inventory_size: int = 0
    inventory_reason: str | None = None
    reconciled: bool = False
    reconciliation_skipped_reason: str | None = None

    def as_counts(self) -> dict[str, Any]:
        """Return a JSON-safe mapping for the ``counts`` column."""
        return asdict(self)


@dataclass(slots=True)
class RunHandle:
    """The open run: its id, and the counters the caller fills in."""

    run_id: uuid.UUID
    counters: RunCounters = field(default_factory=RunCounters)


@asynccontextmanager
async def ingestion_run(
    engine: AsyncEngine,
    *,
    mode: RunMode = "sync",
    extractor_version: int,
) -> AsyncGenerator[RunHandle]:
    """Open a run, guard it against overlap, and close it honestly.

    Raises :class:`ConcurrentRunError` before doing anything if another
    run holds the lock. On any exception the run is closed ``failed`` and
    the exception propagates — the caller decides what to do, but the row
    can never be left claiming success.
    """
    session_factory = build_session_factory(engine)

    # A dedicated connection: a session-level advisory lock belongs to one
    # backend, and a pooled session may hand its connection back at every
    # commit. Holding the lock here keeps it for the whole run regardless
    # of how many transactions the work below commits.
    lock_connection = await engine.connect()
    acquired = False
    try:
        acquired = bool(
            await lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": RUN_ADVISORY_LOCK_KEY},
            )
        )
        if not acquired:
            raise ConcurrentRunError("Another ingestion run is already in progress.")

        async with session_factory() as session:
            # Safe without a timeout: the lock is held, so any row still
            # marked running belongs to a process that no longer exists.
            await session.execute(
                update(IngestionRun)
                .where(IngestionRun.status == "running")
                .values(
                    status="aborted",
                    finished_at=datetime.now(UTC),
                    abort_reason="stale run closed by a later run",
                )
            )
            run = IngestionRun(
                mode=mode,
                status="running",
                extractor_version=extractor_version,
                normalizer_version=NORMALIZER_VERSION,
                hash_version=HASH_VERSION,
                chunker_version=CHUNKER_VERSION,
            )
            session.add(run)
            await session.flush()
            handle = RunHandle(run_id=run.id)
            # Committed before any work: a crash must leave evidence.
            await session.commit()

        try:
            yield handle
        except BaseException as exc:
            await _close(session_factory, handle, "failed", type(exc).__name__)
            raise
        else:
            await _close(session_factory, handle, "succeeded", None)
    finally:
        if acquired:
            # Explicit, not implicit. Closing an ``AsyncConnection`` returns
            # it to the pool rather than ending the PostgreSQL backend
            # session, and a session-level advisory lock outlives the pool
            # checkout — so relying on close() leaves the pipeline locked
            # until the process exits. The rollback first clears any failed
            # transaction state so the unlock itself can execute.
            await lock_connection.rollback()
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": RUN_ADVISORY_LOCK_KEY},
            )
            await lock_connection.commit()
        await lock_connection.close()


async def _close(
    session_factory: async_sessionmaker[AsyncSession],
    handle: RunHandle,
    status: Literal["succeeded", "failed"],
    abort_reason: str | None,
) -> None:
    """Write the terminal status and counts for a run."""
    async with session_factory() as session:
        await session.execute(
            update(IngestionRun)
            .where(IngestionRun.id == handle.run_id)
            .values(
                status=status,
                finished_at=datetime.now(UTC),
                counts=handle.counters.as_counts(),
                abort_reason=abort_reason,
            )
        )
        await session.commit()
