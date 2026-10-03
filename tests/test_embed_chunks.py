"""Embedding backfill tests, against the real (test) database.

No test reaches the network: the provider is :class:`Metered`, a
deterministic fake whose behaviour each test scripts. The backfill commits
per batch by design, so the rollback pattern used elsewhere cannot apply:
every document lives under a URI namespace unique to this module and the
fixture removes what it created. Chunk texts carry a per-test token so no
two tests ever share a content hash.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fake_embedder import FakeEmbedder
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from preston.canonical import hash_content
from preston.core.db import build_async_engine, build_session_factory
from preston.embed_chunks import (
    BATCH_SIZE,
    DIMENSIONS,
    IDENTITY,
    BackfillError,
    BackfillIncomplete,
    CostLimitExceeded,
    backfill_embeddings,
    estimate_cost_usd,
)
from preston.embedding import EmbeddingError
from preston.models import Document, DocumentChunk, IngestionRun
from preston.runs import ConcurrentRunError, ingestion_run
from preston.sync import synchronize

PREFIX = "https://backfill.test/"
OTHER_IDENTITY = "openai:text-embedding-3-small:3072"
EXISTING_VECTOR = [0.5] + [0.0] * (DIMENSIONS - 1)
NOW = datetime(2026, 9, 30, tzinfo=UTC)

Vectors = list[list[float]]


class Metered(FakeEmbedder):
    """A fake provider with a failure script, billing counters and a hook.

    ``script`` holds one entry per call: an exception to raise, or ``None``
    to answer normally. ``corrupt`` may reshape the answer; ``on_call`` runs
    inside the call, i.e. *while* the backfill is waiting on the provider.
    """

    def __init__(self, script: Sequence[Exception | None] = ()) -> None:
        super().__init__(DIMENSIONS)
        self.script = list(script)
        self.corrupt: Callable[[Vectors], Vectors] | None = None
        self.on_call: Callable[[], Awaitable[None]] | None = None
        self._requests = 0
        self._prompt_tokens = 0

    @property
    def requests(self) -> int:
        return self._requests

    @property
    def prompt_tokens(self) -> int:
        return self._prompt_tokens

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.on_call is not None:
            await self.on_call()
        if self.script:
            step = self.script.pop(0)
            if step is not None:
                raise step
        self._requests += 1
        self._prompt_tokens += sum(len(text) for text in texts) // 4
        vectors = [self.vector_for(text) for text in texts]
        return self.corrupt(vectors) if self.corrupt else vectors

    @property
    def texts_sent(self) -> list[str]:
        return [text for call in self.calls for text in call]


class Sleeps:
    """A recording stand-in for ``asyncio.sleep``: tests never wait."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def token() -> str:
    return uuid.uuid4().hex


@pytest.fixture
async def engine(real_database_url: str | None) -> AsyncIterator[AsyncEngine]:
    """Yield an engine and remove every row this module's tests created."""
    if real_database_url is None:
        pytest.skip("No local database is reachable.")

    built = build_async_engine(real_database_url)
    factory = build_session_factory(built)
    async with factory() as session:
        before = set((await session.execute(select(IngestionRun.id))).scalars())
        foreign = await session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.embedding.is_(None))
        )
    # The backfill selects by state, so rows from elsewhere would change
    # every count below. Fail loudly instead of asserting around them.
    assert foreign == 0, "the test database holds unembedded chunks of its own"
    try:
        yield built
    finally:
        async with factory() as session, session.begin():
            stale = select(Document.id).where(Document.canonical_uri.like(PREFIX + "%"))
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id.in_(stale))
            )
            await session.execute(
                delete(Document).where(Document.canonical_uri.like(PREFIX + "%"))
            )
            await session.execute(
                delete(IngestionRun).where(IngestionRun.id.notin_(before))
            )
        await built.dispose()


async def seed(
    engine: AsyncEngine,
    token: str,
    slug: str,
    texts: Sequence[str],
    *,
    scope: str = "grc",
    status: str = "active",
    model: str | None = None,
    vector: list[float] | None = None,
) -> uuid.UUID:
    """Insert one document; ``model`` and ``vector`` embed its chunks."""
    async with build_session_factory(engine)() as session, session.begin():
        document = Document(
            canonical_uri=f"{PREFIX}{token}/{slug}",
            source_type="mysql",
            content_type="service",
            source_scope=scope,
            title=f"Title {slug}",
            blocks=[],
            doc_metadata={},
            status=status,
            content_hash=hash_content(f"{token}{slug}"),
            hash_version=1,
            normalizer_version=3,
            extractor_version=1,
            chunker_version=3,
            fetched_at=NOW,
            last_seen_at=NOW,
        )
        session.add(document)
        await session.flush()
        for index, text in enumerate(texts):
            full = f"{token} {text}"
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=full,
                    content_hash=hash_content(full),
                    embedding=vector,
                    embedding_model=model,
                )
            )
        return document.id


async def chunk_rows(engine: AsyncEngine, token: str) -> dict[str, DocumentChunk]:
    """This test's chunks by text (the token prefix removed)."""
    async with build_session_factory(engine)() as session:
        rows = (
            await session.execute(
                select(DocumentChunk)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(Document.canonical_uri.like(f"{PREFIX}{token}/%"))
            )
        ).scalars()
        return {row.content.removeprefix(f"{token} "): row for row in rows}


async def run_rows(engine: AsyncEngine) -> list[IngestionRun]:
    async with build_session_factory(engine)() as session:
        return list(
            (
                await session.execute(
                    select(IngestionRun).where(IngestionRun.mode == "backfill")
                )
            ).scalars()
        )


async def execute(engine: AsyncEngine, embedder: Metered, **options: Any) -> Any:
    options.setdefault("sleep", Sleeps())
    return await backfill_embeddings(engine, embedder, execute=True, **options)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pending_chunks_are_embedded_with_the_production_identity(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one", "two"])
    embedder = Metered()

    report = await execute(engine, embedder)

    rows = await chunk_rows(engine, token)
    assert {r.embedding_model for r in rows.values()} == {IDENTITY}
    assert all(r.embedding is not None for r in rows.values())
    assert report.counters.rows_updated == 2
    assert report.counters.pending_after == 0


@pytest.mark.anyio
async def test_only_active_documents_of_public_scopes_are_selected(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "public", ["p"])
    await seed(engine, token, "private", ["x"], scope="internal-notes")
    await seed(engine, token, "archived", ["y"], status="archived")
    embedder = Metered()

    await execute(engine, embedder)

    rows = await chunk_rows(engine, token)
    assert rows["p"].embedding is not None
    assert rows["x"].embedding is None
    assert rows["y"].embedding is None
    assert embedder.texts_sent == [f"{token} p"]


@pytest.mark.anyio
async def test_already_embedded_chunks_are_skipped_and_untouched(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "done", ["done"], model=IDENTITY, vector=EXISTING_VECTOR)
    await seed(engine, token, "todo", ["todo"])
    embedder = Metered()

    await execute(engine, embedder)

    assert embedder.texts_sent == [f"{token} todo"]
    rows = await chunk_rows(engine, token)
    assert list(rows["done"].embedding or []) == EXISTING_VECTOR


@pytest.mark.anyio
async def test_a_different_model_identity_is_re_embedded(
    engine: AsyncEngine, token: str
) -> None:
    await seed(
        engine, token, "old", ["old"], model=OTHER_IDENTITY, vector=EXISTING_VECTOR
    )

    await execute(engine, Metered())

    row = (await chunk_rows(engine, token))["old"]
    assert row.embedding_model == IDENTITY
    assert list(row.embedding or []) != EXISTING_VECTOR


@pytest.mark.anyio
async def test_duplicate_content_hash_is_embedded_once_and_written_to_every_row(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["same"])
    await seed(engine, token, "b", ["same"])
    embedder = Metered()

    report = await execute(engine, embedder)

    assert embedder.texts_sent == [f"{token} same"]
    assert report.counters.selected_chunks == 2
    assert report.counters.unique_texts == 1
    assert report.counters.rows_updated == 2
    async with build_session_factory(engine)() as session:
        embedded = await session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.content_hash == hash_content(f"{token} same"),
                DocumentChunk.embedding.is_not(None),
            )
        )
    assert embedded == 2


@pytest.mark.anyio
async def test_a_second_run_selects_nothing_and_calls_no_provider(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one", "two"])
    await execute(engine, Metered())
    second = Metered()

    report = await execute(engine, second)

    assert second.calls == []
    assert report.counters.selected_chunks == 0
    assert report.counters.rows_updated == 0


@pytest.mark.anyio
async def test_scope_restricts_the_selection(engine: AsyncEngine, token: str) -> None:
    await seed(engine, token, "g", ["g"], scope="grc")
    await seed(engine, token, "b", ["b"], scope="blog")

    await execute(engine, Metered(), scopes=["blog"])

    rows = await chunk_rows(engine, token)
    assert rows["b"].embedding is not None
    assert rows["g"].embedding is None


@pytest.mark.anyio
async def test_a_non_public_scope_is_refused(engine: AsyncEngine) -> None:
    with pytest.raises(BackfillError):
        await backfill_embeddings(engine, None, scopes=["internal-notes"])


@pytest.mark.anyio
async def test_limit_caps_the_chunks_selected(engine: AsyncEngine, token: str) -> None:
    await seed(engine, token, "a", ["one", "two", "three"])

    report = await execute(engine, Metered(), limit=2)

    assert report.counters.selected_chunks == 2
    assert report.counters.pending_after == 1


# ---------------------------------------------------------------------------
# Dry run and cost guard
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dry_run_is_the_default_and_writes_nothing(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one", "two"])
    runs_before = len(await run_rows(engine))

    report = await backfill_embeddings(engine, None)

    assert report.executed is False
    assert report.run_id is None
    assert report.counters.selected_chunks == 2
    assert report.counters.unique_texts == 2
    characters = sum(len(f"{token} {t}") for t in ("one", "two"))
    assert report.estimated_tokens == -(-characters // 4)
    assert report.estimated_cost_usd == estimate_cost_usd(report.estimated_tokens)
    rows = await chunk_rows(engine, token)
    assert all(r.embedding is None for r in rows.values())
    assert len(await run_rows(engine)) == runs_before


@pytest.mark.anyio
async def test_dry_run_never_calls_the_provider_even_if_one_is_given(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])
    embedder = Metered()

    await backfill_embeddings(engine, embedder)

    assert embedder.calls == []


@pytest.mark.anyio
async def test_cost_guard_refuses_before_any_provider_call(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])
    embedder = Metered()

    with pytest.raises(CostLimitExceeded):
        await execute(engine, embedder, max_cost_usd=0.0)

    assert embedder.calls == []
    rows = await chunk_rows(engine, token)
    assert rows["one"].embedding is None
    (run,) = await run_rows(engine)
    assert run.status == "failed"
    assert run.abort_reason == "CostLimitExceeded"


@pytest.mark.anyio
async def test_execute_without_a_provider_is_refused(engine: AsyncEngine) -> None:
    with pytest.raises(BackfillError):
        await backfill_embeddings(engine, None, execute=True)


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_batches_never_exceed_the_batch_limit(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", [f"t{n}" for n in range(BATCH_SIZE + 2)])
    embedder = Metered()

    report = await execute(engine, embedder)

    assert [len(call) for call in embedder.calls] == [BATCH_SIZE, 2]
    assert report.counters.batches == 2
    assert report.counters.embedded_texts == BATCH_SIZE + 2


@pytest.mark.anyio
async def test_a_batch_size_above_the_limit_is_refused(engine: AsyncEngine) -> None:
    with pytest.raises(BackfillError):
        await backfill_embeddings(engine, None, batch_size=BATCH_SIZE + 1)


# ---------------------------------------------------------------------------
# Retries, failure and resume
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_transient_errors_are_retried_with_backoff(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])
    embedder = Metered([ConnectionError(), TimeoutError(), None])
    sleeps = Sleeps()

    report = await execute(engine, embedder, sleep=sleeps)

    assert sleeps.delays == [2.0, 4.0]
    assert len(embedder.calls) == 3
    assert report.counters.failed_batches == 0
    assert report.counters.rows_updated == 1


@pytest.mark.anyio
async def test_retries_stop_after_three_and_the_batch_counts_as_failed(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])
    embedder = Metered([ConnectionError()] * 10)
    sleeps = Sleeps()

    with pytest.raises(BackfillIncomplete) as stopped:
        await execute(engine, embedder, sleep=sleeps)

    assert sleeps.delays == [2.0, 4.0, 8.0]
    assert len(embedder.calls) == 4  # the first try and three retries
    assert stopped.value.counters.failed_batches == 1
    assert stopped.value.counters.stop_reason == "batches_failed"
    assert (await chunk_rows(engine, token))["one"].embedding is None


@pytest.mark.anyio
async def test_three_failed_batches_in_a_row_stop_the_job(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["a", "b", "c", "d"])
    embedder = Metered([ConnectionError()] * 100)

    with pytest.raises(BackfillIncomplete) as stopped:
        await execute(engine, embedder, batch_size=1, retry_delays=())

    counters = stopped.value.counters
    assert counters.failed_batches == 3
    assert counters.stop_reason == "consecutive_failures"
    assert len(embedder.calls) == 3  # the fourth batch was never attempted
    assert counters.pending_after == 4


@pytest.mark.anyio
async def test_a_success_resets_the_consecutive_failure_count(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["a", "b", "c", "d", "e"])
    # Batches go: fail, fail, ok, fail, ok — three failures, never in a row.
    embedder = Metered([ConnectionError(), ConnectionError(), None, ConnectionError()])

    with pytest.raises(BackfillIncomplete) as stopped:
        await execute(engine, embedder, batch_size=1, retry_delays=())

    counters = stopped.value.counters
    assert len(embedder.calls) == 5  # every batch was attempted
    assert counters.embedded_texts == 2
    assert counters.failed_batches == 3
    assert counters.stop_reason == "batches_failed"


@pytest.mark.anyio
async def test_a_non_retryable_error_stops_immediately(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["a", "b"])
    embedder = Metered([ValueError("bad request")])
    sleeps = Sleeps()

    with pytest.raises(BackfillIncomplete) as stopped:
        await execute(engine, embedder, batch_size=1, sleep=sleeps)

    assert sleeps.delays == []
    assert len(embedder.calls) == 1
    assert stopped.value.counters.stop_reason == "non_retryable_error"


@pytest.mark.anyio
async def test_a_failed_job_resumes_from_what_is_still_pending(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one", "two", "three"])
    with pytest.raises(BackfillIncomplete):
        await execute(engine, Metered([None, ValueError("stop")]), batch_size=1)
    rows = await chunk_rows(engine, token)
    assert sum(r.embedding is not None for r in rows.values()) == 1

    resumed = Metered()
    report = await execute(engine, resumed, batch_size=1)

    assert len(resumed.texts_sent) == 2
    assert report.counters.pending_after == 0
    rows = await chunk_rows(engine, token)
    assert all(r.embedding is not None for r in rows.values())


@pytest.mark.anyio
async def test_a_failed_batch_leaves_the_database_untouched(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])

    with pytest.raises(BackfillIncomplete):
        await execute(engine, Metered([ValueError("stop")]))

    assert (await chunk_rows(engine, token))["one"].embedding is None


# ---------------------------------------------------------------------------
# Malformed provider responses
# ---------------------------------------------------------------------------


def drop_one(vectors: Vectors) -> Vectors:
    return vectors[:-1]


def shorten(vectors: Vectors) -> Vectors:
    return [vector[:-1] for vector in vectors]


def with_nan(vectors: Vectors) -> Vectors:
    return [[float("nan"), *vector[1:]] for vector in vectors]


def with_inf(vectors: Vectors) -> Vectors:
    return [[float("inf"), *vector[1:]] for vector in vectors]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "corrupt",
    [drop_one, shorten, with_nan, with_inf],
    ids=["count", "dimension", "nan", "inf"],
)
async def test_a_malformed_response_is_never_written(
    engine: AsyncEngine,
    token: str,
    corrupt: Callable[[Vectors], Vectors],
) -> None:
    await seed(engine, token, "a", ["one", "two"])
    embedder = Metered()
    embedder.corrupt = corrupt
    sleeps = Sleeps()

    with pytest.raises(BackfillIncomplete) as stopped:
        await execute(engine, embedder, sleep=sleeps)

    assert stopped.value.counters.stop_reason == "non_retryable_error"
    assert sleeps.delays == []  # a malformed answer is not retried
    rows = await chunk_rows(engine, token)
    assert all(r.embedding is None for r in rows.values())


@pytest.mark.anyio
async def test_an_embedding_error_is_not_retryable(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])
    embedder = Metered([EmbeddingError("malformed")])

    with pytest.raises(BackfillIncomplete):
        await execute(engine, embedder)

    assert len(embedder.calls) == 1


# ---------------------------------------------------------------------------
# Mid-run change protection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_vector_never_lands_on_text_that_changed_mid_run(
    engine: AsyncEngine, token: str
) -> None:
    """The chunk is re-chunked while the provider call is in flight."""
    document_id = await seed(engine, token, "a", ["before"])
    changed = f"{token} after"

    async def rewrite() -> None:
        async with build_session_factory(engine)() as session, session.begin():
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            session.add(
                DocumentChunk(
                    document_id=document_id,
                    chunk_index=0,
                    content=changed,
                    content_hash=hash_content(changed),
                )
            )

    embedder = Metered()
    embedder.on_call = rewrite

    report = await execute(engine, embedder)

    rows = await chunk_rows(engine, token)
    assert set(rows) == {"after"}
    assert rows["after"].embedding is None
    assert report.counters.rows_updated == 0
    assert report.counters.pending_after == 1


# ---------------------------------------------------------------------------
# Run tracking and concurrency
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_run_is_recorded_with_model_identity_and_counters(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one", "two"])

    report = await execute(engine, Metered())

    (run,) = await run_rows(engine)
    assert run.id == report.run_id
    assert run.status == "succeeded"
    assert run.mode == "backfill"
    assert run.extractor_version is None
    assert run.embedding_model == IDENTITY
    counts = run.counts
    assert counts["selected_chunks"] == 2
    assert counts["rows_updated"] == 2
    assert counts["api_requests"] == 1
    assert counts["prompt_tokens"] > 0
    assert counts["pending_after"] == 0
    assert counts["stop_reason"] is None


@pytest.mark.anyio
async def test_a_stopped_run_is_closed_failed_with_its_counters(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])

    with pytest.raises(BackfillIncomplete):
        await execute(engine, Metered([ValueError("stop")]))

    (run,) = await run_rows(engine)
    assert run.status == "failed"
    assert run.abort_reason == "BackfillIncomplete"
    assert run.counts["stop_reason"] == "non_retryable_error"


@pytest.mark.anyio
async def test_an_overlapping_backfill_is_refused(
    engine: AsyncEngine, token: str
) -> None:
    await seed(engine, token, "a", ["one"])
    embedder = Metered()

    async with ingestion_run(engine, extractor_version=1):
        with pytest.raises(ConcurrentRunError):
            await execute(engine, embedder)
        # A dry run takes no lock, so it is not blocked.
        report = await backfill_embeddings(engine, None)

    assert embedder.calls == []
    assert report.counters.selected_chunks == 1


@pytest.mark.anyio
async def test_synchronize_rejects_backfill_mode_before_any_run_row(
    engine: AsyncEngine,
) -> None:
    runs_before = len(await run_rows(engine))

    with pytest.raises(ValueError, match="backfill"):
        await synchronize(engine, cast(Any, None), mode="backfill")

    assert len(await run_rows(engine)) == runs_before


# ---------------------------------------------------------------------------
# Nothing sensitive is logged
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_logs_carry_neither_provider_messages_nor_chunk_text(
    engine: AsyncEngine, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    await seed(engine, token, "a", ["confidential chunk wording"])
    secret = "sk-test-not-a-real-key-leaked"
    embedder = Metered([ConnectionError(secret)] * 10)

    with caplog.at_level(logging.DEBUG), pytest.raises(BackfillIncomplete):
        await execute(engine, embedder)

    assert caplog.records
    assert secret not in caplog.text
    assert "confidential chunk wording" not in caplog.text
    assert "ConnectionError" in caplog.text
