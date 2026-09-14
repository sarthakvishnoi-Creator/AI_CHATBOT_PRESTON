"""Synchronization engine tests, against the real database.

These prove the invariants that matter when the source changes between
runs: convergence, failure preservation, completeness-gated
reconciliation, run honesty, and overlap protection.

Each test uses a canonical URI namespace unique to itself, so tests never
observe each other's rows, and the fixture removes everything it created.
``synchronize`` commits per document by design, so the rollback pattern
used elsewhere in the suite cannot apply here.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from preston.canonical import Paragraph, SourceType
from preston.core.db import build_async_engine, build_session_factory
from preston.models import Document, DocumentChunk, IngestionRun
from preston.runs import RUN_ADVISORY_LOCK_KEY, ConcurrentRunError, ingestion_run
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    IncompleteInventory,
    Inventory,
    SourceRecord,
)
from preston.sync import synchronize

RETRIEVED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class FakeAdapter:
    """A source whose behaviour each test dictates exactly.

    Stands in for the real adapters that arrive in later phases. The
    engine cannot tell the difference: it only ever sees the contract.
    """

    source_type: SourceType = "mysql"
    extractor_version: int = 1

    def __init__(
        self,
        items: list[SourceRecord | ExtractionFailure],
        inventory: Inventory,
        *,
        extractor_version: int = 1,
        inventory_error: Exception | None = None,
    ) -> None:
        self._items = items
        self._inventory = inventory
        self.extractor_version = extractor_version
        self._inventory_error = inventory_error

    async def inventory(self) -> Inventory:
        if self._inventory_error is not None:
            raise self._inventory_error
        return self._inventory

    async def records(self) -> AsyncIterator[SourceRecord | ExtractionFailure]:
        for item in self._items:
            yield item


@pytest.fixture
async def engine(real_database_url: str | None) -> AsyncIterator[AsyncEngine]:
    """Yield an engine and remove every row the test created."""
    if real_database_url is None:
        pytest.skip("No local database is reachable.")

    built = build_async_engine(real_database_url)
    factory = build_session_factory(built)
    async with factory() as session:
        before = set((await session.execute(select(IngestionRun.id))).scalars())
    try:
        yield built
    finally:
        async with factory() as session, session.begin():
            stale = select(Document.id).where(
                Document.canonical_uri.like("https://sync.test/%")
            )
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id.in_(stale))
            )
            await session.execute(
                delete(Document).where(
                    Document.canonical_uri.like("https://sync.test/%")
                )
            )
            await session.execute(
                delete(IngestionRun).where(IngestionRun.id.notin_(before))
            )
        await built.dispose()


@pytest.fixture
def namespace() -> str:
    """A canonical URI prefix unique to one test."""
    return f"https://sync.test/{uuid.uuid4().hex}"


def record(
    uri: str,
    *,
    title: str = "Title",
    text_body: str = "hello world",
    extractor_version: int = 1,
) -> SourceRecord:
    """Build a valid source record for ``uri``."""
    return SourceRecord(
        canonical_uri=uri,
        source_type="mysql",
        content_type="blog",
        title=title,
        blocks=(Paragraph(text=text_body),),
        retrieved_at=RETRIEVED_AT,
        extractor_version=extractor_version,
        source_ref="table#1",
    )


async def _document(engine: AsyncEngine, uri: str) -> Document | None:
    async with build_session_factory(engine)() as session:
        return await session.scalar(
            select(Document).where(Document.canonical_uri == uri)
        )


async def _run(engine: AsyncEngine, run_id: uuid.UUID) -> IngestionRun:
    async with build_session_factory(engine)() as session:
        run = await session.scalar(
            select(IngestionRun).where(IngestionRun.id == run_id)
        )
        assert run is not None
        return run


# ---------------------------------------------------------------------------
# State transitions (invariants 5, 6, 7, 18)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unknown_identity_is_new(engine: AsyncEngine, namespace: str) -> None:
    uri = f"{namespace}/a"
    adapter = FakeAdapter([record(uri)], CompleteInventory(frozenset({uri})))

    report = await synchronize(engine, adapter)

    assert report.counters.new == 1
    stored = await _document(engine, uri)
    assert stored is not None
    assert stored.title == "Title"


@pytest.mark.anyio
async def test_identical_second_run_is_unchanged(
    engine: AsyncEngine, namespace: str
) -> None:
    """Re-running an unmodified source must not rewrite anything."""
    uri = f"{namespace}/a"
    adapter = FakeAdapter([record(uri)], CompleteInventory(frozenset({uri})))

    await synchronize(engine, adapter)
    first = await _document(engine, uri)
    assert first is not None
    first_id, first_hash, first_blocks = first.id, first.content_hash, first.blocks
    async with build_session_factory(engine)() as session:
        first_chunks = [
            (c.chunk_index, c.content, c.id)
            for c in (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == first_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars()
        ]

    report = await synchronize(engine, adapter)

    assert report.counters.unchanged == 1
    assert report.counters.changed == 0
    second = await _document(engine, uri)
    assert second is not None
    # The row is touched (last_seen_at moves) but no content is rewritten:
    # same identity, same hash, same blocks, and the very same chunk rows.
    assert second.id == first_id
    assert second.content_hash == first_hash
    assert second.blocks == first_blocks
    async with build_session_factory(engine)() as session:
        second_chunks = [
            (c.chunk_index, c.content, c.id)
            for c in (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == first_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars()
        ]
    assert second_chunks == first_chunks


@pytest.mark.anyio
async def test_modified_content_is_changed(engine: AsyncEngine, namespace: str) -> None:
    uri = f"{namespace}/a"
    inventory = CompleteInventory(frozenset({uri}))
    await synchronize(engine, FakeAdapter([record(uri)], inventory))

    report = await synchronize(
        engine, FakeAdapter([record(uri, text_body="different")], inventory)
    )

    assert report.counters.changed == 1
    stored = await _document(engine, uri)
    assert stored is not None
    assert "different" in str(stored.blocks)


@pytest.mark.anyio
async def test_repeated_runs_converge_without_duplicates(
    engine: AsyncEngine, namespace: str
) -> None:
    """Idempotency: three runs of one source leave exactly one document."""
    uri = f"{namespace}/a"
    adapter = FakeAdapter([record(uri)], CompleteInventory(frozenset({uri})))

    for _ in range(3):
        await synchronize(engine, adapter)

    async with build_session_factory(engine)() as session:
        rows = list(
            (
                await session.execute(
                    select(Document).where(Document.canonical_uri == uri)
                )
            ).scalars()
        )
    assert len(rows) == 1


@pytest.mark.anyio
async def test_new_identity_alongside_existing_is_new(
    engine: AsyncEngine, namespace: str
) -> None:
    first, second = f"{namespace}/a", f"{namespace}/b"
    await synchronize(
        engine, FakeAdapter([record(first)], CompleteInventory(frozenset({first})))
    )

    report = await synchronize(
        engine,
        FakeAdapter(
            [record(first), record(second)],
            CompleteInventory(frozenset({first, second})),
        ),
    )

    assert report.counters.new == 1
    assert report.counters.unchanged == 1


@pytest.mark.anyio
async def test_extractor_version_change_is_reprocessed_not_changed(
    engine: AsyncEngine, namespace: str
) -> None:
    """A rule change must be distinguishable from a corpus change."""
    uri = f"{namespace}/a"
    inventory = CompleteInventory(frozenset({uri}))
    await synchronize(engine, FakeAdapter([record(uri)], inventory))

    report = await synchronize(
        engine,
        FakeAdapter([record(uri, extractor_version=2)], inventory, extractor_version=2),
    )

    assert report.counters.reprocessed == 1
    assert report.counters.changed == 0


# ---------------------------------------------------------------------------
# Missing reconciliation and completeness gating (invariants 8, 9, 20)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_complete_inventory_flags_missing_candidate(
    engine: AsyncEngine, namespace: str
) -> None:
    """A record absent from a complete inventory becomes a candidate."""
    gone, kept = f"{namespace}/gone", f"{namespace}/kept"
    await synchronize(
        engine,
        FakeAdapter(
            [record(gone), record(kept)], CompleteInventory(frozenset({gone, kept}))
        ),
    )

    report = await synchronize(
        engine, FakeAdapter([record(kept)], CompleteInventory(frozenset({kept})))
    )

    assert report.missing == (gone,)
    assert report.counters.missing_candidates == 1
    stored = await _document(engine, gone)
    assert stored is not None
    # Flagged, never destroyed: content and lifecycle are untouched.
    assert stored.gone_count == 1
    assert stored.status == "active"
    assert stored.title == "Title"


@pytest.mark.anyio
async def test_incomplete_inventory_never_reconciles(
    engine: AsyncEngine, namespace: str
) -> None:
    """An outage must not read as a deletion."""
    gone, kept = f"{namespace}/gone", f"{namespace}/kept"
    await synchronize(
        engine,
        FakeAdapter(
            [record(gone), record(kept)], CompleteInventory(frozenset({gone, kept}))
        ),
    )

    report = await synchronize(
        engine,
        FakeAdapter([record(kept)], IncompleteInventory(reason="source timeout")),
    )

    assert report.missing == ()
    assert report.counters.missing_candidates == 0
    assert report.counters.reconciled is False
    assert report.counters.inventory_complete is False
    assert report.counters.inventory_reason == "source timeout"
    stored = await _document(engine, gone)
    assert stored is not None
    assert stored.gone_count == 0
    assert stored.status == "active"


@pytest.mark.anyio
async def test_empty_complete_inventory_does_not_wipe_the_corpus(
    engine: AsyncEngine, namespace: str
) -> None:
    """A source that answers with nothing is refused, not obeyed."""
    uri = f"{namespace}/a"
    await synchronize(
        engine, FakeAdapter([record(uri)], CompleteInventory(frozenset({uri})))
    )

    report = await synchronize(engine, FakeAdapter([], CompleteInventory(frozenset())))

    assert report.missing == ()
    assert report.counters.reconciled is False
    assert (
        report.counters.reconciliation_skipped_reason
        == "empty inventory while active documents exist"
    )
    stored = await _document(engine, uri)
    assert stored is not None
    assert stored.gone_count == 0


@pytest.mark.anyio
async def test_returning_record_clears_gone_count(
    engine: AsyncEngine, namespace: str
) -> None:
    """A candidate that reappears is fully reinstated."""
    uri = f"{namespace}/a"
    full = CompleteInventory(frozenset({uri}))
    await synchronize(engine, FakeAdapter([record(uri)], full))
    await synchronize(engine, FakeAdapter([], CompleteInventory(frozenset({"x"}))))
    assert (await _document(engine, uri) or Document()).gone_count == 1

    await synchronize(engine, FakeAdapter([record(uri)], full))

    stored = await _document(engine, uri)
    assert stored is not None
    assert stored.gone_count == 0


# ---------------------------------------------------------------------------
# Failure preserves knowledge (invariants 10, 11, 12 of the brief)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_source_failure_preserves_existing_documents(
    engine: AsyncEngine, namespace: str
) -> None:
    """An unavailable source fails the run and touches nothing."""
    uri = f"{namespace}/a"
    await synchronize(
        engine, FakeAdapter([record(uri)], CompleteInventory(frozenset({uri})))
    )
    before = await _document(engine, uri)
    assert before is not None

    broken = FakeAdapter(
        [], CompleteInventory(frozenset()), inventory_error=RuntimeError("source down")
    )
    with pytest.raises(RuntimeError):
        await synchronize(engine, broken)

    after = await _document(engine, uri)
    assert after is not None
    assert after.content_hash == before.content_hash
    assert after.title == before.title
    assert after.gone_count == 0


@pytest.mark.anyio
async def test_extraction_failure_preserves_content_and_counts(
    engine: AsyncEngine, namespace: str
) -> None:
    """A record the source could not produce keeps its stored copy."""
    uri = f"{namespace}/a"
    inventory = CompleteInventory(frozenset({uri}))
    await synchronize(engine, FakeAdapter([record(uri)], inventory))
    before = await _document(engine, uri)
    assert before is not None

    report = await synchronize(
        engine,
        FakeAdapter(
            [ExtractionFailure(canonical_uri=uri, reason="row unreadable")], inventory
        ),
    )

    assert report.counters.extraction_failures == 1
    after = await _document(engine, uri)
    assert after is not None
    assert after.content_hash == before.content_hash
    assert after.consecutive_failure_count == 1
    # Present in the inventory, so never a missing candidate.
    assert after.gone_count == 0


@pytest.mark.anyio
async def test_validation_failure_preserves_content(
    engine: AsyncEngine, namespace: str
) -> None:
    """Invalid extracted content must not become the stored content."""
    uri = f"{namespace}/a"
    inventory = CompleteInventory(frozenset({uri}))
    await synchronize(engine, FakeAdapter([record(uri)], inventory))
    before = await _document(engine, uri)
    assert before is not None

    report = await synchronize(
        engine, FakeAdapter([record(uri, title="", text_body="")], inventory)
    )

    assert report.counters.validation_failures == 1
    assert report.counters.changed == 0
    after = await _document(engine, uri)
    assert after is not None
    assert after.content_hash == before.content_hash
    assert after.title == "Title"
    assert after.consecutive_failure_count == 1


@pytest.mark.anyio
async def test_persistence_failure_is_contained_and_rolled_back(
    engine: AsyncEngine, namespace: str
) -> None:
    """A row that violates a constraint leaves no partial state behind."""
    good, bad = f"{namespace}/good", f"{namespace}/bad"
    broken = SourceRecord(
        canonical_uri=bad,
        source_type="mysql",
        # Violates ck_documents_content_type at the database level.
        content_type=cast(Paragraph, "not-a-content-type"),  # type: ignore[arg-type]
        title="Bad",
        blocks=(Paragraph(text="text"),),
        retrieved_at=RETRIEVED_AT,
        extractor_version=1,
    )
    adapter = FakeAdapter(
        [record(good), broken], CompleteInventory(frozenset({good, bad}))
    )

    report = await synchronize(engine, adapter)

    assert report.counters.persistence_failures == 1
    # The good record either side of the failure still committed.
    assert report.counters.new == 1
    assert await _document(engine, good) is not None
    assert await _document(engine, bad) is None


@pytest.mark.anyio
async def test_successful_record_clears_previous_failure_count(
    engine: AsyncEngine, namespace: str
) -> None:
    uri = f"{namespace}/a"
    inventory = CompleteInventory(frozenset({uri}))
    await synchronize(engine, FakeAdapter([record(uri)], inventory))
    await synchronize(
        engine, FakeAdapter([ExtractionFailure(uri, "unreadable")], inventory)
    )
    assert (await _document(engine, uri) or Document()).consecutive_failure_count == 1

    await synchronize(engine, FakeAdapter([record(uri)], inventory))

    stored = await _document(engine, uri)
    assert stored is not None
    assert stored.consecutive_failure_count == 0


# ---------------------------------------------------------------------------
# Run lifecycle (invariants 15, 16, 17)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_successful_run_is_recorded_with_counts(
    engine: AsyncEngine, namespace: str
) -> None:
    uri = f"{namespace}/a"
    report = await synchronize(
        engine, FakeAdapter([record(uri)], CompleteInventory(frozenset({uri})))
    )

    run = await _run(engine, report.run_id)
    assert run.status == "succeeded"
    assert run.mode == "sync"
    assert run.finished_at is not None
    assert run.abort_reason is None
    assert run.counts["new"] == 1
    assert run.counts["inventory_complete"] is True
    assert run.extractor_version == 1


@pytest.mark.anyio
async def test_failed_run_is_recorded_as_failed(
    engine: AsyncEngine, namespace: str
) -> None:
    """A failed run must never be mistakable for a successful one."""
    adapter = FakeAdapter(
        [], CompleteInventory(frozenset()), inventory_error=RuntimeError("source down")
    )
    with pytest.raises(RuntimeError):
        await synchronize(engine, adapter)

    async with build_session_factory(engine)() as session:
        run = await session.scalar(
            select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(1)
        )
    assert run is not None
    assert run.status == "failed"
    assert run.finished_at is not None


@pytest.mark.anyio
async def test_interrupted_run_is_swept_by_the_next_run(
    engine: AsyncEngine, namespace: str
) -> None:
    """A row left running by a dead process is closed, not trusted."""
    uri = f"{namespace}/a"
    async with build_session_factory(engine)() as session, session.begin():
        orphan = IngestionRun(
            mode="sync",
            status="running",
            extractor_version=1,
            normalizer_version=1,
            hash_version=1,
            chunker_version=1,
        )
        session.add(orphan)
        await session.flush()
        orphan_id = orphan.id

    await synchronize(
        engine, FakeAdapter([record(uri)], CompleteInventory(frozenset({uri})))
    )

    swept = await _run(engine, orphan_id)
    assert swept.status == "aborted"
    assert swept.abort_reason == "stale run closed by a later run"
    assert swept.finished_at is not None


@pytest.mark.anyio
async def test_run_row_exists_while_the_run_is_in_flight(
    engine: AsyncEngine,
) -> None:
    """The row is committed before work starts, so a crash leaves evidence."""
    async with ingestion_run(engine, extractor_version=1) as handle:
        async with build_session_factory(engine)() as session:
            live = await session.scalar(
                select(IngestionRun).where(IngestionRun.id == handle.run_id)
            )
        assert live is not None
        assert live.status == "running"
        assert live.finished_at is None


# ---------------------------------------------------------------------------
# Concurrency and credential safety (invariants 22, 21)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_overlapping_run_is_refused(engine: AsyncEngine) -> None:
    """The second run exits cleanly instead of corrupting the first."""
    async with ingestion_run(engine, extractor_version=1):
        with pytest.raises(ConcurrentRunError):
            await synchronize(engine, FakeAdapter([], CompleteInventory(frozenset())))


@pytest.mark.anyio
async def test_advisory_lock_is_released_after_a_run(engine: AsyncEngine) -> None:
    """A finished run must not leave the pipeline locked."""
    async with ingestion_run(engine, extractor_version=1):
        pass

    async with build_session_factory(engine)() as session:
        acquired = await session.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": RUN_ADVISORY_LOCK_KEY}
        )
        await session.scalar(
            text("SELECT pg_advisory_unlock(:key)"), {"key": RUN_ADVISORY_LOCK_KEY}
        )
    assert acquired is True


@pytest.mark.anyio
async def test_failure_reason_never_stores_credentials(engine: AsyncEngine) -> None:
    """An exception message may carry a connection URL; the run must not."""
    secret = "mysql+pymysql://user:sup3rs3cret@host:3306/db"

    adapter = FakeAdapter(
        [],
        CompleteInventory(frozenset()),
        inventory_error=RuntimeError(f"connection failed for {secret}"),
    )
    with pytest.raises(RuntimeError):
        await synchronize(engine, adapter)

    async with build_session_factory(engine)() as session:
        run = await session.scalar(
            select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(1)
        )
    assert run is not None
    assert run.abort_reason == "RuntimeError"
    assert "sup3rs3cret" not in str(run.abort_reason)
    assert "sup3rs3cret" not in str(run.counts)
    assert "sup3rs3cret" not in str(run.notes)
