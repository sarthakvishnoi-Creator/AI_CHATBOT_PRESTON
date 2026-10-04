"""Tests for the application schema and its ORM models.

The database tests run inside a transaction that is never committed, so
they exercise real PostgreSQL constraint enforcement without leaving rows
behind or requiring a dedicated test database.
"""

import importlib.util
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from preston.canonical import hash_content
from preston.core.db import Base, build_async_engine, build_session_factory
from preston.models import (
    EMBEDDING_DIMENSIONS,
    Conversation,
    Document,
    DocumentChunk,
    IngestionRun,
    Message,
)

EXPECTED_TABLES = {
    "conversations",
    "messages",
    "tool_calls",
    "message_evidence",
    "support_requests",
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


def make_chunk(
    document_id: uuid.UUID, content: str, **overrides: object
) -> DocumentChunk:
    """Build a document_chunks row whose hash matches its content."""
    values: dict[str, object] = {
        "document_id": document_id,
        "chunk_index": 0,
        "content": content,
        "content_hash": hash_content(content),
    }
    values.update(overrides)
    return DocumentChunk(**values)


def test_metadata_defines_the_expected_tables() -> None:
    """The ORM registers exactly the approved tables, and no others."""
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_the_only_vector_column_is_the_chunk_embedding() -> None:
    """Vectors live beside chunk text and nowhere else."""
    vector_columns = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if "vec" in str(column.type).lower()
    }
    assert vector_columns == {"document_chunks.embedding"}


def test_chunk_embedding_columns_are_declared() -> None:
    columns = DocumentChunk.__table__.columns

    assert columns["content_hash"].nullable is False
    assert columns["embedding"].nullable is True
    assert columns["embedding_model"].nullable is True
    embedding_type = columns["embedding"].type
    assert isinstance(embedding_type, HALFVEC)
    assert embedding_type.dim == EMBEDDING_DIMENSIONS == 3072


def test_chunk_embedding_constraint_and_hash_index_are_declared() -> None:
    table = Base.metadata.tables["document_chunks"]
    assert "ck_document_chunks_embedding_model" in {
        constraint.name for constraint in table.constraints
    }
    assert {"ix_document_chunks_content_hash"} <= {
        index.name for index in table.indexes
    }


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
        Message(
            conversation_id=uuid.uuid4(), role="user", content="orphan turn", sequence=1
        )
    )

    with pytest.raises(IntegrityError, match="foreign key"):
        await session.flush()


@pytest.mark.anyio
async def test_deleting_a_conversation_deletes_its_messages(
    session: AsyncSession,
) -> None:
    """``ON DELETE CASCADE`` removes a thread's turns with the thread."""
    conversation = Conversation(visitor_token_hash=hash_content("token"))
    session.add(conversation)
    await session.flush()
    session.add(
        Message(conversation_id=conversation.id, role="user", content="hi", sequence=1)
    )
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
    conversation = Conversation(visitor_token_hash=hash_content("token"))
    session.add(conversation)
    await session.flush()

    session.add(
        Message(
            conversation_id=conversation.id, role="moderator", content="no", sequence=1
        )
    )

    with pytest.raises(IntegrityError, match="ck_messages_role"):
        await session.flush()


@pytest.mark.anyio
async def test_chunk_index_is_unique_within_a_document(session: AsyncSession) -> None:
    """A document cannot hold two chunks claiming the same position."""
    document = make_document()
    session.add(document)
    await session.flush()
    session.add(make_chunk(document.id, "first"))
    await session.flush()

    session.add(make_chunk(document.id, "duplicate"))

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
    session.add(make_chunk(document.id, "a"))
    await session.flush()

    await session.delete(document)
    await session.flush()

    remaining = await session.execute(
        text("SELECT count(*) FROM document_chunks WHERE document_id = :did"),
        {"did": document.id},
    )
    assert remaining.scalar_one() == 0


# ---------------------------------------------------------------------------
# Embedding schema (Phase 7A)
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "openai:text-embedding-3-large:3072"

_EMBEDDING_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "54b5ec347e84_embedding_schema_foundation.py"
)


def _migration_hash_expression() -> str:
    """The exact SQL the migration used to fill ``content_hash``."""
    spec = importlib.util.spec_from_file_location(
        "embedding_schema_foundation", _EMBEDDING_MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expression = vars(module)["_SQL_CONTENT_HASH"]
    assert isinstance(expression, str)
    return expression


def _vector(dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    # Values exactly representable in float16, so a round trip is lossless.
    return [(index % 64) / 64 for index in range(dimensions)]


# Every value is hashed from its exact bytes: no trimming, folding or
# Unicode normalization, which is what these cases would each expose.
HASH_CASES = {
    "ascii": "ISO/IEC 27001:2022 certification",
    "unicode": "Café — naïve 認証 ✅ 🔒 Ωmega",
    "decomposed": "Café (NFD, not NFC)",
    "punctuation": "\"quotes\" 'single' \\x41 \\\\ %s %% ; -- $$ ‘’",
    "whitespace": "  leading\ttab\r\nCRLF\n\ntrailing   nbsp ",
    "long": "Security testing scope. " * 5_000,
}


@pytest.mark.anyio
async def test_live_embedding_columns_match_the_schema(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "FROM pg_attribute a "
            "WHERE a.attrelid = 'document_chunks'::regclass AND a.attnum > 0 "
            "AND a.attname IN ('content_hash', 'embedding', 'embedding_model')"
        )
    )
    columns = {name: (kind, not_null) for name, kind, not_null in result}

    assert columns == {
        "content_hash": ("text", True),
        "embedding": ("halfvec(3072)", False),
        "embedding_model": ("text", False),
    }


@pytest.mark.anyio
async def test_live_content_hash_index_is_a_plain_btree(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename = 'document_chunks'"
        )
    )
    indexes = dict(result.tuples().all())

    assert "USING btree (content_hash)" in indexes["ix_document_chunks_content_hash"]
    # Exact search first: no approximate or full-text index exists yet.
    assert not any(
        method in definition.lower()
        for definition in indexes.values()
        for method in ("hnsw", "ivfflat", "gin")
    )


@pytest.mark.anyio
async def test_a_chunk_without_an_embedding_is_valid(session: AsyncSession) -> None:
    document = make_document(canonical_uri="https://example.com/no-embedding")
    session.add(document)
    await session.flush()
    chunk = make_chunk(document.id, "text only")
    session.add(chunk)
    await session.flush()
    await session.refresh(chunk)

    assert chunk.embedding is None
    assert chunk.embedding_model is None


@pytest.mark.anyio
async def test_a_chunk_with_an_embedding_and_its_model_is_valid(
    session: AsyncSession,
) -> None:
    document = make_document(canonical_uri="https://example.com/embedded")
    session.add(document)
    await session.flush()
    chunk = make_chunk(
        document.id, "embedded", embedding=_vector(), embedding_model=EMBEDDING_MODEL
    )
    session.add(chunk)
    await session.flush()
    await session.refresh(chunk)

    assert chunk.embedding == _vector()
    assert chunk.embedding_model == EMBEDDING_MODEL


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("embedding", "embedding_model"),
    [(_vector(), None), (None, EMBEDDING_MODEL)],
    ids=["vector-without-model", "model-without-vector"],
)
async def test_the_database_rejects_an_embedding_without_its_model(
    session: AsyncSession,
    embedding: list[float] | None,
    embedding_model: str | None,
) -> None:
    """Enforced by PostgreSQL's CHECK, not by the ORM."""
    document = make_document(canonical_uri="https://example.com/half-embedded")
    session.add(document)
    await session.flush()
    session.add(
        make_chunk(
            document.id,
            "half",
            embedding=embedding,
            embedding_model=embedding_model,
        )
    )

    with pytest.raises(IntegrityError, match="ck_document_chunks_embedding_model"):
        await session.flush()


@pytest.mark.anyio
async def test_the_database_rejects_a_vector_of_the_wrong_dimension(
    session: AsyncSession,
) -> None:
    document = make_document(canonical_uri="https://example.com/wrong-dimension")
    session.add(document)
    await session.flush()
    session.add(
        make_chunk(
            document.id,
            "small",
            embedding=_vector(1536),
            embedding_model=EMBEDDING_MODEL,
        )
    )

    with pytest.raises(DBAPIError, match="expected 3072 dimensions"):
        await session.flush()


@pytest.mark.anyio
async def test_a_backfill_run_may_omit_the_extractor_version(
    session: AsyncSession,
) -> None:
    run = IngestionRun(
        mode="backfill",
        status="running",
        extractor_version=None,
        normalizer_version=1,
        hash_version=1,
        chunker_version=1,
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    assert run.extractor_version is None


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["sync", "reprocess"])
async def test_every_other_run_still_requires_an_extractor_version(
    session: AsyncSession, mode: str
) -> None:
    session.add(
        IngestionRun(
            mode=mode,
            status="running",
            extractor_version=None,
            normalizer_version=1,
            hash_version=1,
            chunker_version=1,
        )
    )

    with pytest.raises(IntegrityError, match="ck_ingestion_runs_extractor_version"):
        await session.flush()


@pytest.mark.anyio
@pytest.mark.parametrize("content", HASH_CASES.values(), ids=HASH_CASES.keys())
async def test_the_migration_sql_hash_equals_hash_content(
    session: AsyncSession, content: str
) -> None:
    """The backfill expression reproduces ``hash_content`` byte for byte."""
    document = make_document(canonical_uri="https://example.com/sql-hash")
    session.add(document)
    await session.flush()
    chunk = make_chunk(document.id, content, content_hash="placeholder")
    session.add(chunk)
    await session.flush()

    await session.execute(
        text(
            f"UPDATE document_chunks SET content_hash = {_migration_hash_expression()} "
            "WHERE id = :id"
        ),
        {"id": chunk.id},
    )
    await session.refresh(chunk)

    assert chunk.content == content
    assert chunk.content_hash == hash_content(content)


@pytest.mark.anyio
async def test_every_stored_chunk_hash_matches_hash_content(
    session: AsyncSession,
) -> None:
    """Holds for every row in whatever database the suite is pointed at.

    The migration applies the same check to the real corpus when it runs;
    this keeps it true for rows written afterwards.
    """
    result = await session.execute(
        text("SELECT content, content_hash FROM document_chunks")
    )

    assert all(stored == hash_content(content) for content, stored in result.tuples())
