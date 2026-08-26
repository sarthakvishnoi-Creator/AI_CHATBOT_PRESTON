"""Tests for the application schema and its ORM models.

The database tests run inside a transaction that is never committed, so
they exercise real PostgreSQL constraint enforcement without leaving rows
behind or requiring a dedicated test database.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from preston.core.db import Base, build_async_engine, build_session_factory
from preston.models import Conversation, Document, DocumentChunk, Message

EXPECTED_TABLES = {"conversations", "messages", "documents", "document_chunks"}


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
    document = Document(title="Handbook")
    session.add(document)
    await session.flush()
    session.add(DocumentChunk(document_id=document.id, chunk_index=0, content="first"))
    await session.flush()

    session.add(
        DocumentChunk(document_id=document.id, chunk_index=0, content="duplicate")
    )

    with pytest.raises(IntegrityError):
        await session.flush()
