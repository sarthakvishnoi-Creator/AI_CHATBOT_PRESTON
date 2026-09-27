"""Tests for the application schema and its ORM models.

The database tests run inside a transaction that is never committed, so
they exercise real PostgreSQL constraint enforcement without leaving rows
behind or requiring a dedicated test database.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from preston.core.db import Base, build_async_engine, build_session_factory
from preston.models import (
    Conversation,
    Document,
    DocumentChunk,
    IngestionRun,
    Message,
)

EXPECTED_TABLES = {
    "conversations",
    "messages",
    "documents",
    "document_chunks",
    "ingestion_runs",
}

RETRIEVED_AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def make_document(**overrides: object) -> Document:
    """Build a documents row with every required column populated."""
    values: dict[str, object] = {
        "canonical_uri": "https://www.intercert.com/blogs/handbook",
        "source_type": "mysql",
        "source_ref": "subone_newblogs#1",
        "content_type": "blog",
        "source_scope": "blog",
        "title": "Handbook",
        "blocks": [],
        "content_hash": "deadbeef",
        "hash_version": 1,
        "normalizer_version": 1,
        "extractor_version": 1,
        "chunker_version": 1,
        "fetched_at": RETRIEVED_AT,
        "last_seen_at": RETRIEVED_AT,
    }
    values.update(overrides)
    return Document(**values)


def test_metadata_defines_the_expected_tables() -> None:
    """The ORM registers exactly the approved tables, and no others."""
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_no_vector_column_is_defined_yet() -> None:
    """No embedding model is approved, so no table may carry a vector column."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert "vector" not in str(column.type).lower()


@pytest.fixture
async def session(real_database_url: str | None) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction is rolled back afterwards."""
    if real_database_url is None:
        pytest.skip("No local .env with POSTGRES_* credentials is available.")

    engine = build_async_engine(real_database_url)
    try:
        async with build_session_factory(engine)() as open_session:
            yield open_session
            await open_session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_migration_created_the_expected_tables(session: AsyncSession) -> None:
    """The migration produced the tables the models describe."""
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    )
    present = {row[0] for row in result}

    assert EXPECTED_TABLES <= present


@pytest.mark.anyio
async def test_a_message_requires_an_existing_conversation(
    session: AsyncSession,
) -> None:
    """The foreign key rejects a message pointing at no conversation."""
    session.add(
        Message(conversation_id=uuid.uuid4(), role="user", content="orphan turn")
    )

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_deleting_a_conversation_deletes_its_messages(
    session: AsyncSession,
) -> None:
    """``ON DELETE CASCADE`` removes a thread's turns with the thread."""
    conversation = Conversation()
    session.add(conversation)
    await session.flush()
    session.add(Message(conversation_id=conversation.id, role="user", content="hello"))
    await session.flush()

    await session.delete(conversation)
    await session.flush()

    remaining = await session.execute(
        text("SELECT count(*) FROM messages WHERE conversation_id = :cid"),
        {"cid": conversation.id},
    )
    assert remaining.scalar_one() == 0


@pytest.mark.anyio
async def test_an_unknown_message_role_is_rejected(session: AsyncSession) -> None:
    """The CHECK constraint confines roles to the approved set."""
    conversation = Conversation()
    session.add(conversation)
    await session.flush()

    session.add(
        Message(conversation_id=conversation.id, role="moderator", content="nope")
    )

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_chunk_index_is_unique_within_a_document(session: AsyncSession) -> None:
    """A document cannot hold two chunks claiming the same position."""
    document = make_document()
    session.add(document)
    await session.flush()
    session.add(DocumentChunk(document_id=document.id, chunk_index=0, content="first"))
    await session.flush()

    session.add(
        DocumentChunk(document_id=document.id, chunk_index=0, content="duplicate")
    )

    with pytest.raises(IntegrityError):
        await session.flush()


# ---------------------------------------------------------------------------
# Canonical identity, lifecycle and run bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_canonical_uri_is_unique(session: AsyncSession) -> None:
    """Identity is the canonical URI: one public page, one row."""
    session.add(make_document(canonical_uri="https://example.com/unique"))
    await session.flush()

    session.add(make_document(canonical_uri="https://example.com/unique", title="Copy"))

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_an_unknown_source_type_is_rejected(session: AsyncSession) -> None:
    """Three adapters are known at build time; a CHECK models that."""
    session.add(make_document(source_type="scraper"))

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_an_unknown_content_type_is_rejected(session: AsyncSession) -> None:
    """Content type is derived from the source family, never guessed."""
    session.add(make_document(content_type="newsletter"))

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_only_two_lifecycle_states_are_persistable(
    session: AsyncSession,
) -> None:
    """There is deliberately no ``failed`` state: failure is counted, not applied."""
    session.add(make_document(status="failed"))

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_a_document_defaults_to_active_with_clean_counters(
    session: AsyncSession,
) -> None:
    """The database, not the caller, decides what a fresh document looks like."""
    document = make_document(canonical_uri="https://example.com/defaults")
    session.add(document)
    await session.flush()
    await session.refresh(document)

    assert document.status == "active"
    assert document.language == "en"
    assert document.gone_count == 0
    assert document.consecutive_failure_count == 0
    assert document.doc_metadata == {}
    assert document.last_run_id is None


@pytest.mark.anyio
async def test_an_unknown_run_mode_is_rejected(session: AsyncSession) -> None:
    """Three run modes, one flag, no new infrastructure."""
    session.add(
        IngestionRun(
            mode="continuous",
            status="running",
            extractor_version=1,
            normalizer_version=1,
            hash_version=1,
            chunker_version=1,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_an_unknown_run_status_is_rejected(session: AsyncSession) -> None:
    """A run is running, succeeded, failed or aborted."""
    session.add(
        IngestionRun(
            mode="sync",
            status="paused",
            extractor_version=1,
            normalizer_version=1,
            hash_version=1,
            chunker_version=1,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.anyio
async def test_deleting_a_run_does_not_delete_its_documents(
    session: AsyncSession,
) -> None:
    """Losing run history must never lose knowledge."""
    run = IngestionRun(
        mode="sync",
        status="succeeded",
        extractor_version=1,
        normalizer_version=1,
        hash_version=1,
        chunker_version=1,
    )
    session.add(run)
    await session.flush()
    document = make_document(
        canonical_uri="https://example.com/survives", last_run_id=run.id
    )
    session.add(document)
    await session.flush()

    await session.delete(run)
    await session.flush()
    await session.refresh(document)

    assert document.last_run_id is None
    assert document.title == "Handbook"


@pytest.mark.anyio
async def test_deleting_a_document_deletes_its_chunks(session: AsyncSession) -> None:
    """Chunks are derived from a document and do not outlive it."""
    document = make_document(canonical_uri="https://example.com/cascade")
    session.add(document)
    await session.flush()
    session.add(DocumentChunk(document_id=document.id, chunk_index=0, content="a"))
    await session.flush()

    await session.delete(document)
    await session.flush()

    remaining = await session.execute(
        text("SELECT count(*) FROM document_chunks WHERE document_id = :did"),
        {"did": document.id},
    )
    assert remaining.scalar_one() == 0
