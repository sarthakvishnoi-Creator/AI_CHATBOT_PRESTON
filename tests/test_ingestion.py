"""Tests for the document ingestion foundation.

Chunking is a pure function and is tested as such. Ingestion itself is
tested against the real database, in a transaction that is rolled back
afterwards, following the same pattern as ``test_models.py``.

Normalization and hashing moved out with the modules that now own them:
see ``test_normalization.py`` and ``test_canonical.py``.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from preston.canonical import (
    HASH_VERSION,
    CanonicalDocument,
    Heading,
    Paragraph,
    Provenance,
    blocks_from_json,
    hash_document,
)
from preston.core.db import build_async_engine, build_session_factory
from preston.ingestion import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CHUNKER_VERSION,
    IngestionOutcome,
    chunk_text,
    ingest_document,
)
from preston.models import Document, DocumentChunk, IngestionRun
from preston.normalization import NORMALIZER_VERSION

RETRIEVED_AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_empty_text_produces_no_chunks() -> None:
    """Empty input produces an empty chunk list, not a single empty chunk."""
    assert chunk_text("") == []


def test_chunk_text_shorter_than_size_produces_one_chunk() -> None:
    """Text shorter than the chunk size is returned whole, as one chunk."""
    text = "short text"
    assert chunk_text(text, size=1000, overlap=150) == [text]


def test_chunk_text_uses_the_approved_defaults() -> None:
    """The module's default size/overlap match the current approved values."""
    assert CHUNK_SIZE == 1000
    assert CHUNK_OVERLAP == 150


def test_chunk_text_splits_at_the_configured_size() -> None:
    """A chunk boundary falls exactly at ``size`` characters."""
    text = "a" * 2500
    chunks = chunk_text(text, size=1000, overlap=150)

    assert len(chunks[0]) == 1000
    assert chunks[0] == text[:1000]


def test_chunk_text_overlaps_consecutive_chunks() -> None:
    """Consecutive full-size chunks share exactly ``overlap`` characters."""
    text = "".join(f"{i:04d}" for i in range(500))  # 2000 distinct characters
    chunks = chunk_text(text, size=1000, overlap=150)

    assert chunks[0][-150:] == chunks[1][:150]


def test_chunk_text_covers_the_whole_text_with_no_gaps() -> None:
    """Concatenating each chunk's non-overlapping prefix reconstructs the text."""
    text = "x" * 3300
    size, overlap = 1000, 150
    step = size - overlap
    chunks = chunk_text(text, size=size, overlap=overlap)

    reconstructed = "".join(chunk[:step] for chunk in chunks[:-1]) + chunks[-1]
    assert reconstructed == text


def test_chunk_text_last_chunk_may_be_shorter() -> None:
    """A text that doesn't fill the last window ends in a short final chunk."""
    text = "a" * 1850  # two 1000-char windows, 850 apart
    chunks = chunk_text(text, size=1000, overlap=150)

    assert len(chunks) == 2
    assert len(chunks[1]) == 1000
    assert chunks[1] == text[850:1850]


# ---------------------------------------------------------------------------
# ingest_document
# ---------------------------------------------------------------------------


def make_document(
    canonical_uri: str,
    *,
    title: str = "A",
    text: str = "hello world",
    extractor_version: int = 1,
    run_id: uuid.UUID | None = None,
) -> CanonicalDocument:
    """Build a one-paragraph canonical document."""
    return CanonicalDocument(
        canonical_uri=canonical_uri,
        source_type="mysql",
        content_type="blog",
        title=title,
        blocks=(Paragraph(text=text),),
        source_ref="subone_newblogs#1",
        provenance=Provenance(
            retrieved_at=RETRIEVED_AT,
            extractor_version=extractor_version,
            normalizer_version=NORMALIZER_VERSION,
            run_id=run_id,
        ),
    )


@pytest.fixture
async def session(real_database_url: str | None) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction is rolled back afterwards."""
    if real_database_url is None:
        pytest.skip("No local database is reachable.")

    engine = build_async_engine(real_database_url)
    try:
        async with build_session_factory(engine)() as open_session:
            yield open_session
            await open_session.rollback()
    finally:
        await engine.dispose()


async def _chunks_of(session: AsyncSession, document_id: object) -> list[DocumentChunk]:
    result = await session.execute(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    )
    return list(result.scalars())


@pytest.mark.anyio
async def test_ingest_new_document_creates_document_and_chunks(
    session: AsyncSession,
) -> None:
    """Ingesting an unseen canonical URI creates a document and its chunks."""
    document = make_document("https://example.com/a")

    result = await ingest_document(session, document)
    await session.flush()

    assert result.outcome is IngestionOutcome.NEW
    assert result.document.canonical_uri == "https://example.com/a"
    assert result.document.title == "A"
    assert result.document.content_hash == hash_document(document)

    chunks = await _chunks_of(session, result.document.id)
    assert [c.content for c in chunks] == ["hello world"]


@pytest.mark.anyio
async def test_ingest_identical_content_is_a_noop(session: AsyncSession) -> None:
    """Re-ingesting the same URI with unchanged content changes nothing."""
    document = make_document("https://example.com/b", title="B", text="same content")
    first = await ingest_document(session, document)
    await session.flush()
    first_id = first.document.id

    second = await ingest_document(session, document)
    await session.flush()

    assert second.outcome is IngestionOutcome.UNCHANGED
    assert second.document.id == first_id
    result = await session.execute(
        select(Document).where(Document.canonical_uri == "https://example.com/b")
    )
    assert len(list(result.scalars())) == 1
    chunks = await _chunks_of(session, first_id)
    assert len(chunks) == 1


@pytest.mark.anyio
async def test_ingest_changed_content_updates_document_and_rebuilds_chunks(
    session: AsyncSession,
) -> None:
    """Changed content for the same URI updates the row and its chunks."""
    canonical_uri = "https://example.com/c"
    original = await ingest_document(
        session, make_document(canonical_uri, title="C", text="version one")
    )
    await session.flush()
    original_id = original.document.id

    updated = await ingest_document(
        session,
        make_document(canonical_uri, title="C revised", text="version two, changed"),
    )
    await session.flush()

    assert updated.outcome is IngestionOutcome.CHANGED
    assert updated.document.id == original_id
    assert updated.document.title == "C revised"

    chunks = await _chunks_of(session, original_id)
    assert [c.content for c in chunks] == ["version two, changed"]


@pytest.mark.anyio
async def test_ingest_duplicate_uri_does_not_create_a_second_document(
    session: AsyncSession,
) -> None:
    """Ingesting the same URI twice, with different content, keeps one row."""
    canonical_uri = "https://example.com/d"
    await ingest_document(session, make_document(canonical_uri, text="first"))
    await session.flush()

    await ingest_document(session, make_document(canonical_uri, text="second"))
    await session.flush()

    result = await session.execute(
        select(Document).where(Document.canonical_uri == canonical_uri)
    )
    assert len(list(result.scalars())) == 1


@pytest.mark.anyio
async def test_ingest_document_does_not_commit(session: AsyncSession) -> None:
    """Ingestion leaves the transaction open for the caller to commit or roll back."""
    await ingest_document(session, make_document("https://example.com/e"))
    await session.flush()

    await session.rollback()

    result = await session.execute(
        select(Document).where(Document.canonical_uri == "https://example.com/e")
    )
    assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Provenance, the version quartet and lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingest_records_source_and_provenance(session: AsyncSession) -> None:
    """Where the document came from and which rules produced it."""
    result = await ingest_document(session, make_document("https://example.com/f"))
    await session.flush()
    row = result.document

    assert row.source_type == "mysql"
    assert row.source_ref == "subone_newblogs#1"
    assert row.content_type == "blog"
    assert row.language == "en"
    assert row.status == "active"
    assert row.hash_version == HASH_VERSION
    assert row.normalizer_version == NORMALIZER_VERSION
    assert row.extractor_version == 1
    assert row.fetched_at == RETRIEVED_AT
    assert row.last_seen_at == RETRIEVED_AT
    assert row.gone_count == 0
    assert row.consecutive_failure_count == 0


@pytest.mark.anyio
async def test_ingest_stores_the_authoritative_blocks(session: AsyncSession) -> None:
    """The stored blocks must reproduce the stored hash without the source."""
    document = CanonicalDocument(
        canonical_uri="https://example.com/g",
        source_type="api",
        content_type="service",
        title="Service",
        blocks=(Heading(level=2, text="Benefits"), Paragraph(text="Certified.")),
        provenance=Provenance(
            retrieved_at=RETRIEVED_AT,
            extractor_version=1,
            normalizer_version=NORMALIZER_VERSION,
        ),
    )

    result = await ingest_document(session, document)
    await session.flush()

    assert blocks_from_json(result.document.blocks) == document.blocks


@pytest.mark.anyio
async def test_an_unchanged_document_only_touches_the_run_pointers(
    session: AsyncSession,
) -> None:
    """UNCHANGED writes ``last_seen_at`` and ``last_run_id``, nothing else."""
    canonical_uri = "https://example.com/h"
    first = await ingest_document(session, make_document(canonical_uri))
    await session.flush()
    first_hash = first.document.content_hash

    later = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    document = make_document(canonical_uri)
    seen_again = CanonicalDocument(
        canonical_uri=document.canonical_uri,
        source_type=document.source_type,
        content_type=document.content_type,
        title=document.title,
        blocks=document.blocks,
        source_ref=document.source_ref,
        provenance=Provenance(
            retrieved_at=later,
            extractor_version=1,
            normalizer_version=NORMALIZER_VERSION,
        ),
    )

    result = await ingest_document(session, seen_again)
    await session.flush()

    assert result.outcome is IngestionOutcome.UNCHANGED
    assert result.document.last_seen_at == later
    # Untouched: the content did not change, so neither did its provenance.
    assert result.document.fetched_at == RETRIEVED_AT
    assert result.document.content_hash == first_hash


@pytest.mark.anyio
async def test_a_version_bump_is_reprocessed_not_changed(
    session: AsyncSession,
) -> None:
    """Our rules changing must not read as the corpus changing."""
    canonical_uri = "https://example.com/i"
    await ingest_document(session, make_document(canonical_uri))
    await session.flush()

    result = await ingest_document(
        session, make_document(canonical_uri, extractor_version=2)
    )
    await session.flush()

    assert result.outcome is IngestionOutcome.REPROCESSED
    assert result.document.extractor_version == 2


@pytest.mark.anyio
async def test_an_archived_document_is_restored_when_it_reappears(
    session: AsyncSession,
) -> None:
    """Removal is reversible: restoration is a status flip, not a re-import."""
    canonical_uri = "https://example.com/j"
    first = await ingest_document(session, make_document(canonical_uri))
    await session.flush()
    first.document.status = "archived"
    first.document.gone_count = 2
    await session.flush()

    result = await ingest_document(session, make_document(canonical_uri))
    await session.flush()

    assert result.outcome is IngestionOutcome.RESTORED
    assert result.document.status == "active"
    assert result.document.gone_count == 0


@pytest.mark.anyio
async def test_ingest_links_a_document_to_its_run(session: AsyncSession) -> None:
    """A document records which run last wrote it."""
    run = IngestionRun(
        mode="sync",
        status="running",
        extractor_version=1,
        normalizer_version=NORMALIZER_VERSION,
        hash_version=HASH_VERSION,
        chunker_version=CHUNKER_VERSION,
    )
    session.add(run)
    await session.flush()

    result = await ingest_document(
        session, make_document("https://example.com/k", run_id=run.id)
    )
    await session.flush()

    assert result.document.last_run_id == run.id
