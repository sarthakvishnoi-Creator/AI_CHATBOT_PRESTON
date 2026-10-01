"""Tests for ``search_knowledge`` (Phase 8C) against the real test database.

Rows are inserted inside a transaction that is rolled back, with hand-built
3,072-dimension vectors so the expected order is known exactly. The query
vector comes from a scripted embedder: nothing here calls OpenAI. Each test
uses its own embedding-model identity, so no other row can interfere.
"""

import math
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fake_embedder import FakeEmbedder
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from preston.canonical import hash_content
from preston.core.db import build_async_engine, build_session_factory
from preston.models import EMBEDDING_DIMENSIONS, Document, DocumentChunk
from preston.retrieval import (
    CHUNKS_PER_DOCUMENT,
    MAX_QUERY_CHARACTERS,
    OFFICE_SCOPE,
    PUBLIC_SCOPES,
    InvalidQueryError,
    RetrievalError,
    get_office_locations,
    search_knowledge,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
SITE = "https://www.intercert.com/test/"


@pytest.fixture
async def session(real_database_url: str | None) -> AsyncIterator[AsyncSession]:
    """A session whose transaction is rolled back afterwards."""
    if real_database_url is None:
        pytest.skip("No local test database is reachable.")
    engine = build_async_engine(real_database_url)
    try:
        async with build_session_factory(engine)() as open_session:
            yield open_session
            await open_session.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def model() -> str:
    """A model identity used by no other row in the database."""
    return f"test:{uuid.uuid4().hex[:12]}:{EMBEDDING_DIMENSIONS}"


def unit(*weights: tuple[int, float]) -> list[float]:
    """A 3,072-dimension vector with the given (axis, weight) components."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for axis, weight in weights:
        vector[axis] = weight
    return vector


class Scripted:
    """An embedder that returns one chosen vector for every query."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]


class Failing:
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise ConnectionError("provider said: sk-test-not-a-real-key leaked")


async def add_document(
    session: AsyncSession,
    slug: str,
    chunks: Sequence[tuple[str, list[float] | None]],
    *,
    model: str,
    scope: str = "grc",
    status: str = "active",
    uri: str | None = None,
    metadata: dict[str, Any] | None = None,
    chunk_model: str | None = None,
) -> Document:
    """Insert a document and its chunks; ``None`` leaves a chunk unembedded."""
    document = Document(
        canonical_uri=uri or SITE + slug,
        source_type="mysql",
        content_type="service",
        source_scope=scope,
        title=f"Title {slug}",
        blocks=[],
        doc_metadata=metadata or {},
        status=status,
        content_hash=hash_content(slug),
        hash_version=1,
        normalizer_version=3,
        extractor_version=1,
        chunker_version=3,
        fetched_at=NOW,
        last_seen_at=NOW,
    )
    session.add(document)
    await session.flush()
    for index, (content, vector) in enumerate(chunks):
        session.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=index,
                content=content,
                content_hash=hash_content(content),
                embedding=vector,
                embedding_model=None if vector is None else (chunk_model or model),
            )
        )
    await session.flush()
    return document


QUERY = unit((0, 1.0))


# ---------------------------------------------------------------------------
# Ranking, score and grouping
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_nearest_chunk_ranks_first(session: AsyncSession, model: str) -> None:
    await add_document(session, "far", [("far chunk", unit((1, 1.0)))], model=model)
    await add_document(
        session, "near", [("near chunk", unit((0, 1.0), (1, 0.1)))], model=model
    )
    await add_document(
        session, "mid", [("mid chunk", unit((0, 1.0), (1, 1.0)))], model=model
    )

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert [e.text for e in results] == ["near chunk", "mid chunk", "far chunk"]
    assert [e.rank for e in results] == [1, 2, 3]


@pytest.mark.anyio
async def test_the_score_is_cosine_similarity(
    session: AsyncSession, model: str
) -> None:
    await add_document(
        session, "a", [("diagonal", unit((0, 1.0), (1, 1.0)))], model=model
    )

    (result,) = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    # cos(45°); halfvec storage is half precision, hence the tolerance.
    assert result.score == pytest.approx(1 / math.sqrt(2), abs=1e-3)


@pytest.mark.anyio
async def test_at_most_two_chunks_per_document_are_kept(
    session: AsyncSession, model: str
) -> None:
    await add_document(
        session,
        "big",
        [(f"big {n}", unit((0, 1.0), (1, n / 10))) for n in range(5)],
        model=model,
    )

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert CHUNKS_PER_DOCUMENT == 2
    assert [e.text for e in results] == ["big 0", "big 1"]


@pytest.mark.anyio
async def test_documents_are_ordered_by_their_best_chunk(
    session: AsyncSession, model: str
) -> None:
    await add_document(
        session,
        "one",
        [("one best", unit((0, 1.0), (1, 0.1))), ("one weak", unit((1, 1.0)))],
        model=model,
    )
    await add_document(session, "two", [("two", unit((0, 1.0), (1, 0.5)))], model=model)

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    # A document's chunks stay together, after its best chunk.
    assert [e.text for e in results] == ["one best", "one weak", "two"]


@pytest.mark.anyio
async def test_ties_are_broken_deterministically(
    session: AsyncSession, model: str
) -> None:
    for slug in ("t1", "t2", "t3"):
        await add_document(
            session, slug, [(f"tie {slug}", unit((0, 1.0)))], model=model
        )

    first = await search_knowledge(session, Scripted(QUERY), "q", embedding_model=model)
    second = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert [e.chunk_content_hash for e in first] == [
        e.chunk_content_hash for e in second
    ]
    assert [e.chunk_content_hash for e in first] == sorted(
        e.chunk_content_hash for e in first
    )


@pytest.mark.anyio
async def test_the_documents_limit_is_respected(
    session: AsyncSession, model: str
) -> None:
    for n in range(5):
        await add_document(
            session, f"d{n}", [(f"d{n}", unit((0, 1.0), (1, n)))], model=model
        )

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model, documents=2
    )

    assert [e.text for e in results] == ["d0", "d1"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_public_scopes_are_searched(
    session: AsyncSession, model: str
) -> None:
    await add_document(
        session,
        "internal",
        [("internal", unit((0, 1.0)))],
        model=model,
        scope="internal_crm",
    )
    await add_document(session, "public", [("public", unit((1, 1.0)))], model=model)

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert [e.text for e in results] == ["public"]
    assert "internal_crm" not in PUBLIC_SCOPES


@pytest.mark.anyio
async def test_archived_documents_are_excluded(
    session: AsyncSession, model: str
) -> None:
    await add_document(
        session, "gone", [("archived", unit((0, 1.0)))], model=model, status="archived"
    )
    await add_document(session, "live", [("active", unit((1, 1.0)))], model=model)

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert [e.text for e in results] == ["active"]


@pytest.mark.anyio
async def test_unembedded_chunks_are_excluded(
    session: AsyncSession, model: str
) -> None:
    await add_document(
        session,
        "mixed",
        [("not embedded", None), ("embedded", unit((1, 1.0)))],
        model=model,
    )

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert [e.text for e in results] == ["embedded"]


@pytest.mark.anyio
async def test_other_models_embeddings_are_excluded(
    session: AsyncSession, model: str
) -> None:
    await add_document(
        session,
        "other",
        [("other model", unit((0, 1.0)))],
        model=model,
        chunk_model="test:another-model:3072",
    )
    await add_document(session, "mine", [("this model", unit((1, 1.0)))], model=model)

    results = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert [e.text for e in results] == ["this model"]


@pytest.mark.anyio
async def test_scopes_narrow_and_an_empty_match_is_not_an_error(
    session: AsyncSession, model: str
) -> None:
    await add_document(session, "grc", [("grc chunk", unit((0, 1.0)))], model=model)

    narrowed = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model, scopes=["grc"]
    )
    nothing = await search_knowledge(
        session,
        Scripted(QUERY),
        "q",
        embedding_model=model,
        scopes=["office_locations"],
    )

    assert [e.text for e in narrowed] == ["grc chunk"]
    assert nothing == []


@pytest.mark.anyio
async def test_scopes_can_never_widen_beyond_the_public_list(
    session: AsyncSession, model: str
) -> None:
    with pytest.raises(InvalidQueryError, match="Not public"):
        _ = await search_knowledge(
            session,
            Scripted(QUERY),
            "q",
            embedding_model=model,
            scopes=["internal_crm"],
        )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_evidence_is_fully_populated(session: AsyncSession, model: str) -> None:
    document = await add_document(
        session, "pci", [("PCI DSS protects card data.", unit((0, 1.0)))], model=model
    )

    (result,) = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert result.text == "PCI DSS protects card data."
    assert result.chunk_content_hash == hash_content("PCI DSS protects card data.")
    assert result.canonical_uri == SITE + "pci" == result.citation_uri
    assert (result.title, result.source_scope, result.content_type) == (
        "Title pci",
        "grc",
        "service",
    )
    assert (result.chunk_index, result.rank, result.retrieval_method) == (
        0,
        1,
        "vector",
    )
    assert result.embedding_model == model
    assert result.document_content_hash == document.content_hash
    assert result.score == pytest.approx(1.0, abs=1e-3)


@pytest.mark.anyio
async def test_image_citations_follow_the_mapping(
    session: AsyncSession, model: str
) -> None:
    parent = "https://www.intercert.com/services/governance-risk-compliance/pci-dss"
    await add_document(
        session,
        "confirmed",
        [("confirmed diagram", unit((0, 1.0)))],
        model=model,
        scope="image_descriptions",
        uri="preston-image://intercert/Intercert_Img/confirmed.png",
        metadata={
            "source": {
                "mapping": {
                    "status": "confirmed",
                    "service_document": {"canonical_uri": parent},
                }
            }
        },
    )
    await add_document(
        session,
        "unresolved",
        [("unresolved diagram", unit((0, 1.0), (1, 0.5)))],
        model=model,
        scope="image_descriptions",
        uri="preston-image://intercert/Intercert_Img/unresolved.png",
        metadata={"source": {"mapping": {"status": "unresolved"}}},
    )

    confirmed, unresolved = await search_knowledge(
        session, Scripted(QUERY), "q", embedding_model=model
    )

    assert confirmed.citation_uri == parent
    assert unresolved.citation_uri is None
    assert unresolved.canonical_uri.startswith("preston-image://")


# ---------------------------------------------------------------------------
# Validation and failures
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
async def test_an_empty_query_is_rejected_before_embedding(
    session: AsyncSession, model: str, query: str
) -> None:
    embedder = Scripted(QUERY)

    with pytest.raises(InvalidQueryError, match="empty"):
        _ = await search_knowledge(session, embedder, query, embedding_model=model)
    assert embedder.calls == []


@pytest.mark.anyio
async def test_an_overlong_query_is_rejected(session: AsyncSession, model: str) -> None:
    with pytest.raises(InvalidQueryError, match="longer than"):
        _ = await search_knowledge(
            session,
            Scripted(QUERY),
            "x" * (MAX_QUERY_CHARACTERS + 1),
            embedding_model=model,
        )


@pytest.mark.anyio
async def test_the_query_is_embedded_stripped(
    session: AsyncSession, model: str
) -> None:
    await add_document(session, "a", [("a", unit((0, 1.0)))], model=model)
    embedder = Scripted(QUERY)

    _ = await search_knowledge(
        session, embedder, "  What is PCI DSS?  ", embedding_model=model
    )

    assert embedder.calls == [["What is PCI DSS?"]]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "embedder",
    [FakeEmbedder(dimensions=1536), Scripted(unit((0, math.nan)))],
    ids=["wrong-dimension", "non-finite"],
)
async def test_a_malformed_query_vector_is_an_error(
    session: AsyncSession, model: str, embedder: Any
) -> None:
    with pytest.raises(RetrievalError, match="malformed"):
        _ = await search_knowledge(session, embedder, "q", embedding_model=model)


@pytest.mark.anyio
async def test_an_embedder_failure_hides_the_providers_text(
    session: AsyncSession, model: str
) -> None:
    with pytest.raises(RetrievalError) as failure:
        _ = await search_knowledge(session, Failing(), "q", embedding_model=model)

    assert "ConnectionError" in str(failure.value)
    assert "sk-test" not in str(failure.value)
    assert failure.value.__cause__ is None and failure.value.__suppress_context__


@pytest.mark.anyio
async def test_zero_coverage_is_a_clear_error(
    session: AsyncSession, model: str
) -> None:
    await add_document(session, "plain", [("not embedded", None)], model=model)

    with pytest.raises(RetrievalError, match="No public chunk is embedded with"):
        _ = await search_knowledge(session, Scripted(QUERY), "q", embedding_model=model)


@pytest.mark.anyio
async def test_a_database_failure_is_a_retrieval_error() -> None:
    class BrokenSession:
        async def execute(self, *_: object) -> object:
            raise OperationalError("SELECT 1", {}, Exception("server closed"))

    with pytest.raises(RetrievalError, match="OperationalError") as failure:
        _ = await search_knowledge(
            cast(AsyncSession, BrokenSession()),
            Scripted(QUERY),
            "q",
            embedding_model="test:x:3072",
        )
    assert "server closed" not in str(failure.value)


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_only_reads(session: AsyncSession, model: str) -> None:
    await add_document(
        session, "r", [("r0", unit((0, 1.0))), ("r1", unit((1, 1.0)))], model=model
    )
    fingerprint = text(
        "SELECT count(*), md5(string_agg(id::text || content_hash || "
        "coalesce(embedding_model, ''), ',' ORDER BY id)) FROM document_chunks"
    )
    before = (await session.execute(fingerprint)).one()
    statements: list[str] = []
    engine = cast(AsyncEngine, session.bind).sync_engine

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        _ = await search_knowledge(session, Scripted(QUERY), "q", embedding_model=model)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert (await session.execute(fingerprint)).one() == before
    assert statements and all(
        s.lstrip().upper().startswith("SELECT") for s in statements
    )
    assert not session.new and not session.dirty and not session.deleted


# ---------------------------------------------------------------------------
# Phase 8D: get_office_locations
# ---------------------------------------------------------------------------

OFFICE_URI = "https://www.intercert.com/contactus#office-locations"
OFFICE_CHUNKS = [
    "INTERCERT Asia & Middle East Corporate Office\n\n10th Floor, Noida, India",
    "INTERCERT Japan\n\n1-8-14 Kitashinagawa, Shinagawa-ku, Tokyo, Japan",
    "INTERCERT INC – Headquarters\n\n2001 Timberloch Place, The Woodlands, Texas",
]


async def add_offices(
    session: AsyncSession, *, status: str = "active", vectors: bool = False
) -> Document:
    return await add_document(
        session,
        "offices",
        [(c, unit((0, 1.0)) if vectors else None) for c in OFFICE_CHUNKS],
        model="test:offices:3072",
        scope=OFFICE_SCOPE,
        uri=OFFICE_URI,
        status=status,
    )


@pytest.mark.anyio
async def test_the_office_tool_returns_every_chunk_in_order(
    session: AsyncSession,
) -> None:
    await add_offices(session)

    results = [
        e for e in await get_office_locations(session) if e.canonical_uri == OFFICE_URI
    ]

    assert [e.text for e in results] == OFFICE_CHUNKS
    assert [e.chunk_index for e in results] == [0, 1, 2]
    assert [e.rank for e in results] == [
        1,
        2,
        3,
    ]  # the test database holds no other offices


@pytest.mark.anyio
async def test_office_evidence_is_structured_with_no_score(
    session: AsyncSession,
) -> None:
    document = await add_offices(session)

    results = [
        e for e in await get_office_locations(session) if e.canonical_uri == OFFICE_URI
    ]

    for evidence in results:
        assert evidence.retrieval_method == "structured"
        assert evidence.score is None and evidence.embedding_model is None
        assert evidence.source_scope == OFFICE_SCOPE
        assert evidence.citation_uri == OFFICE_URI
        assert evidence.document_content_hash == document.content_hash
    assert results[2].chunk_content_hash == hash_content(OFFICE_CHUNKS[2])


@pytest.mark.anyio
async def test_the_office_tool_works_without_embeddings_and_with_them(
    session: AsyncSession,
) -> None:
    await add_offices(session, vectors=False)
    unembedded = [
        e.text
        for e in await get_office_locations(session)
        if e.canonical_uri == OFFICE_URI
    ]

    assert unembedded == OFFICE_CHUNKS

    await session.rollback()
    await add_offices(session, vectors=True)
    embedded = [
        e.text
        for e in await get_office_locations(session)
        if e.canonical_uri == OFFICE_URI
    ]
    assert embedded == OFFICE_CHUNKS


@pytest.mark.anyio
async def test_an_archived_office_document_is_excluded(session: AsyncSession) -> None:
    await add_offices(session, status="archived")

    results = await get_office_locations(session)

    assert OFFICE_URI not in {e.canonical_uri for e in results}


@pytest.mark.anyio
async def test_only_the_office_scope_is_returned(
    session: AsyncSession, model: str
) -> None:
    await add_document(session, "other", [("a grc page", unit((0, 1.0)))], model=model)
    await add_offices(session)

    results = await get_office_locations(session)

    assert {e.source_scope for e in results} == {OFFICE_SCOPE}
    assert "a grc page" not in {e.text for e in results}


@pytest.mark.anyio
async def test_no_office_document_is_an_empty_list_not_an_error(
    session: AsyncSession,
) -> None:
    await session.execute(
        text(
            "DELETE FROM documents WHERE source_scope = 'office_locations' "
            "AND status = 'active'"
        )
    )

    assert await get_office_locations(session) == []


@pytest.mark.anyio
async def test_an_office_database_failure_is_a_retrieval_error() -> None:
    class BrokenSession:
        async def execute(self, *_: object) -> object:
            raise OperationalError("SELECT 1", {}, Exception("server closed"))

    with pytest.raises(RetrievalError, match="OperationalError") as failure:
        _ = await get_office_locations(cast(AsyncSession, BrokenSession()))

    assert "server closed" not in str(failure.value)


@pytest.mark.anyio
async def test_the_office_tool_only_reads(session: AsyncSession) -> None:
    await add_offices(session)
    fingerprint = text(
        "SELECT count(*), md5(string_agg(id::text || content_hash, ',' ORDER BY id)) "
        "FROM document_chunks"
    )
    before = (await session.execute(fingerprint)).one()
    statements: list[str] = []
    engine = cast(AsyncEngine, session.bind).sync_engine

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        _ = await get_office_locations(session)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert (await session.execute(fingerprint)).one() == before
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    assert not session.new and not session.dirty and not session.deleted
