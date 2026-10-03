"""The embedding backfill — fills ``document_chunks.embedding``, nothing else.

Ingestion never calls the embedding provider, so chunks are stored without
vectors. This job embeds them. It touches only ``embedding`` and
``embedding_model`` on chunk rows: no text, hash, document or index changes.

**A dry run is the default.** Without ``--execute`` the job reads the
database, prints what it would embed and what that would cost, and stops: no
provider is constructed, no lock is taken, no run row is written. Writing
requires ``--execute``::

    uv run python -m preston.embed_chunks                       # dry run
    uv run python -m preston.embed_chunks --execute --scope image_descriptions

**Pending work is selected by state, so resuming is free.** A chunk is
pending when it sits in an active document of a public scope and has no
embedding, or one made by a different model identity. A rerun after any
interruption simply selects what is still pending; a finished corpus selects
nothing and the job exits having called no provider.

**A vector is written against the text it was made from.** Chunks are grouped
by ``content_hash`` and each distinct text is embedded once. The write is
``UPDATE … WHERE content_hash = …`` restricted to still-pending rows of active
public documents. If a document is re-chunked while the job runs, its old
rows are gone and its new rows carry different hashes, so the stale vector
matches nothing rather than landing on changed text. Each stored chunk's hash
is re-verified against its text before anything is sent to the provider.

**Embedding never happens inside a database transaction.** A batch is sent,
validated, and only then written in one short transaction; a failed batch
leaves the database exactly as it was.

**Retries are explicit.** The SDK's own retries are off. A transient failure
(connection, timeout, rate limit, provider error) is retried up to three more
times after 2, 4 and 8 seconds; anything else stops the job immediately, as
do three batches in a row that exhausted their retries. A stopped job closes
its run ``failed`` and exits non-zero; completed batches stay written.

**Nothing sensitive is logged.** Failures are reported by exception class
name only, never the provider's message, and chunk text is never logged.
"""

import argparse
import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sqlalchemy import ColumnElement, CursorResult, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from preston.canonical import hash_content
from preston.core.config import Settings, get_settings
from preston.core.db import build_async_engine, build_session_factory
from preston.core.errors import PrestonError
from preston.core.logging import configure_logging
from preston.embedding import (
    Embedder,
    EmbeddingError,
    OpenAIEmbedder,
    embedding_identity,
    is_retryable,
)
from preston.models import Document, DocumentChunk
from preston.retrieval import PUBLIC_SCOPES
from preston.runs import BackfillCounters, ConcurrentRunError, backfill_run

logger = logging.getLogger(__name__)

# The approved production model. Fixed here rather than read from settings so
# a changed environment cannot change what this job writes.
MODEL = "text-embedding-3-large"
DIMENSIONS = 3072
IDENTITY = embedding_identity(MODEL, DIMENSIONS)

BATCH_SIZE = 128
REQUEST_TIMEOUT_SECONDS = 60.0
RETRY_DELAYS_SECONDS = (2.0, 4.0, 8.0)
MAX_CONSECUTIVE_FAILED_BATCHES = 3
DEFAULT_MAX_COST_USD = 1.0

# Planning estimate only (the provider bills real tokens): about four
# characters per token, at $0.13 per million tokens for text-embedding-3-large.
CHARACTERS_PER_TOKEN = 4
USD_PER_MILLION_TOKENS = 0.13

Sleep = Callable[[float], Awaitable[None]]


class MeteredEmbedder(Embedder, Protocol):
    """An :class:`Embedder` that reports what the provider billed."""

    @property
    def requests(self) -> int: ...

    @property
    def prompt_tokens(self) -> int: ...


class BackfillError(PrestonError):
    """The backfill cannot proceed safely; nothing was embedded for it."""

    title = "Backfill Error"


class CostLimitExceeded(BackfillError):
    """The estimated cost is above ``--max-cost-usd``; the provider was not called."""


class BackfillIncomplete(BackfillError):
    """The job stopped before embedding everything it planned.

    Carries the counters so the caller can report them; the run row has
    already been closed ``failed`` with the same counters.
    """

    def __init__(self, counters: BackfillCounters) -> None:
        super().__init__("The embedding backfill stopped before finishing.")
        self.counters = counters


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """The outcome of one invocation, dry run or executed."""

    run_id: uuid.UUID | None
    executed: bool
    counters: BackfillCounters
    estimated_tokens: int
    estimated_cost_usd: float
    cost_limit_usd: float


@dataclass(frozen=True, slots=True)
class _Plan:
    """Pending chunks, grouped: one entry per distinct text."""

    selected_chunks: int
    texts: list[tuple[str, str]]  # (content_hash, content)

    @property
    def estimated_tokens(self) -> int:
        characters = sum(len(content) for _, content in self.texts)
        return math.ceil(characters / CHARACTERS_PER_TOKEN)


def estimate_cost_usd(tokens: int) -> float:
    return tokens * USD_PER_MILLION_TOKENS / 1_000_000


def _is_pending() -> ColumnElement[bool]:
    """A chunk with no vector, or one from a different model identity."""
    return or_(
        DocumentChunk.embedding.is_(None),
        DocumentChunk.embedding_model != IDENTITY,
    )


def _pending_filters(scopes: Sequence[str]) -> list[ColumnElement[bool]]:
    return [
        Document.status == "active",
        Document.source_scope.in_(sorted(scopes)),
        _is_pending(),
    ]


async def _plan(engine: AsyncEngine, scopes: Sequence[str], limit: int | None) -> _Plan:
    """Read the pending chunks and group them by text. Read-only."""
    query = (
        select(DocumentChunk.content_hash, DocumentChunk.content)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*_pending_filters(scopes))
        .order_by(DocumentChunk.content_hash, DocumentChunk.id)
    )
    if limit is not None:
        query = query.limit(limit)
    async with build_session_factory(engine)() as session:
        rows = (await session.execute(query)).all()

    unique: dict[str, str] = {}
    for content_hash, content in rows:
        if hash_content(content) != content_hash:
            # Never send text whose identity cannot be vouched for.
            raise BackfillError("A stored chunk hash does not match its text.")
        unique.setdefault(content_hash, content)
    return _Plan(selected_chunks=len(rows), texts=list(unique.items()))


async def _count_pending(engine: AsyncEngine, scopes: Sequence[str]) -> int:
    query = (
        select(DocumentChunk.id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*_pending_filters(scopes))
    )
    async with build_session_factory(engine)() as session:
        return len((await session.execute(query)).all())


async def _write_batch(
    engine: AsyncEngine,
    scopes: Sequence[str],
    hashes: Sequence[str],
    vectors: Sequence[list[float]],
) -> int:
    """Store one batch's vectors in a single short transaction."""
    updated = 0
    async with build_session_factory(engine)() as session:
        for content_hash, vector in zip(hashes, vectors, strict=True):
            result = await session.execute(
                update(DocumentChunk)
                .where(
                    DocumentChunk.content_hash == content_hash,
                    DocumentChunk.document_id.in_(
                        select(Document.id).where(
                            Document.status == "active",
                            Document.source_scope.in_(sorted(scopes)),
                        )
                    ),
                    _is_pending(),
                )
                .values(embedding=vector, embedding_model=IDENTITY)
            )
            updated += cast(CursorResult[Any], result).rowcount
        await session.commit()
    return updated


def _check_vectors(vectors: Sequence[Sequence[float]], expected: int) -> None:
    """Refuse to write a batch the provider answered wrongly.

    ``OpenAIEmbedder`` already validates; this is the writer's own check, so
    no :class:`Embedder` can put a misshapen vector into the database.
    """
    if len(vectors) != expected:
        raise EmbeddingError("The provider returned the wrong number of vectors.")
    for vector in vectors:
        if len(vector) != DIMENSIONS:
            raise EmbeddingError("A returned vector has the wrong dimension.")
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingError("A returned vector has a non-finite value.")


async def _embed_with_retry(
    embedder: Embedder, texts: list[str], sleep: Sleep, delays: Sequence[float]
) -> list[list[float]]:
    """One batch, retried only for transient failures."""
    attempt = 0
    while True:
        try:
            return await embedder.embed(texts)
        except Exception as exc:
            if not is_retryable(exc) or attempt >= len(delays):
                raise
            logger.warning(
                "Embedding batch failed (%s); retry %d of %d.",
                type(exc).__name__,
                attempt + 1,
                len(delays),
            )
            await sleep(delays[attempt])
            attempt += 1


async def backfill_embeddings(
    engine: AsyncEngine,
    embedder: MeteredEmbedder | None,
    *,
    scopes: Sequence[str] | None = None,
    limit: int | None = None,
    execute: bool = False,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    batch_size: int = BATCH_SIZE,
    sleep: Sleep = asyncio.sleep,
    retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
) -> BackfillReport:
    """Plan the backfill and, with ``execute``, carry it out.

    Raises :class:`ConcurrentRunError` if another ingestion or backfill run
    holds the lock, :class:`CostLimitExceeded` before any provider call if the
    estimate is over ``max_cost_usd``, and :class:`BackfillIncomplete` if the
    job stopped early. ``embedder`` may be ``None`` only for a dry run.
    """
    chosen = sorted(PUBLIC_SCOPES if scopes is None else set(scopes))
    unknown = [scope for scope in chosen if scope not in PUBLIC_SCOPES]
    if unknown:
        raise BackfillError(f"Not a public scope: {unknown[0]!r}.")
    if not 1 <= batch_size <= BATCH_SIZE:
        raise BackfillError(f"batch_size must be between 1 and {BATCH_SIZE}.")
    if limit is not None and limit < 1:
        raise BackfillError("limit must be at least 1.")

    if not execute:
        plan = await _plan(engine, chosen, limit)
        counters = BackfillCounters(
            selected_chunks=plan.selected_chunks,
            unique_texts=len(plan.texts),
            pending_after=plan.selected_chunks,
        )
        return _report(None, False, counters, plan, max_cost_usd)

    if embedder is None:
        raise BackfillError("An embedder is required to execute.")

    started = time.monotonic()
    async with backfill_run(engine, embedding_model=IDENTITY) as handle:
        counters = handle.counters
        # Planned inside the lock, so what is selected is what nobody else
        # can be changing.
        plan = await _plan(engine, chosen, limit)
        counters.selected_chunks = plan.selected_chunks
        counters.unique_texts = len(plan.texts)

        if estimate_cost_usd(plan.estimated_tokens) > max_cost_usd:
            counters.stop_reason = "cost_limit"
            counters.pending_after = plan.selected_chunks
            raise CostLimitExceeded("Estimated cost exceeds the cost limit.")

        consecutive_failures = 0
        for start in range(0, len(plan.texts), batch_size):
            batch = plan.texts[start : start + batch_size]
            counters.batches += 1
            try:
                vectors = await _embed_with_retry(
                    embedder, [content for _, content in batch], sleep, retry_delays
                )
                _check_vectors(vectors, len(batch))
            except Exception as exc:  # noqa: BLE001
                counters.failed_batches += 1
                consecutive_failures += 1
                logger.error("Embedding batch failed: %s", type(exc).__name__)
                if not is_retryable(exc):
                    counters.stop_reason = "non_retryable_error"
                    break
                if consecutive_failures >= MAX_CONSECUTIVE_FAILED_BATCHES:
                    counters.stop_reason = "consecutive_failures"
                    break
                continue
            consecutive_failures = 0
            counters.rows_updated += await _write_batch(
                engine, chosen, [h for h, _ in batch], vectors
            )
            counters.embedded_texts += len(batch)

        if counters.stop_reason is None and counters.failed_batches:
            counters.stop_reason = "batches_failed"
        counters.api_requests = embedder.requests
        counters.prompt_tokens = embedder.prompt_tokens
        counters.pending_after = await _count_pending(engine, chosen)
        counters.elapsed_seconds = round(time.monotonic() - started, 2)
        if counters.stop_reason is not None:
            raise BackfillIncomplete(counters)

    return _report(handle.run_id, True, counters, plan, max_cost_usd)


def _report(
    run_id: uuid.UUID | None,
    executed: bool,
    counters: BackfillCounters,
    plan: _Plan,
    max_cost_usd: float,
) -> BackfillReport:
    return BackfillReport(
        run_id=run_id,
        executed=executed,
        counters=counters,
        estimated_tokens=plan.estimated_tokens,
        estimated_cost_usd=estimate_cost_usd(plan.estimated_tokens),
        cost_limit_usd=max_cost_usd,
    )


def build_backfill_embedder(settings: Settings) -> OpenAIEmbedder:
    """The production embedder, pinned to the approved model and a 60 s timeout."""
    return OpenAIEmbedder.from_settings(
        settings.model_copy(
            update={
                "embedding_model": MODEL,
                "embedding_dimensions": DIMENSIONS,
                "embedding_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            }
        )
    )


async def run_backfill(
    settings: Settings,
    *,
    scopes: Sequence[str] | None,
    limit: int | None,
    execute: bool,
    max_cost_usd: float,
) -> BackfillReport:
    """Compose the engine and (only when executing) the provider, and run."""
    if settings.database_url is None:
        raise RuntimeError("No database is configured (PRESTON_DATABASE_URL is unset).")
    embedder = build_backfill_embedder(settings) if execute else None
    engine = build_async_engine(str(settings.database_url))
    try:
        return await backfill_embeddings(
            engine,
            embedder,
            scopes=scopes,
            limit=limit,
            execute=execute,
            max_cost_usd=max_cost_usd,
        )
    finally:
        await engine.dispose()


def log_summary(report: BackfillReport) -> None:
    counters = report.counters
    logger.info(
        "Embedding backfill %s: pending_chunks=%d unique_texts=%d "
        "estimated_tokens=%d estimated_cost_usd=%.4f (limit %.2f)",
        "executed" if report.executed else "dry run",
        counters.selected_chunks,
        counters.unique_texts,
        report.estimated_tokens,
        report.estimated_cost_usd,
        report.cost_limit_usd,
    )
    if not report.executed:
        logger.info("Dry run: no provider call and no database write was made.")
        return
    log_counters(counters)


def log_counters(counters: BackfillCounters) -> None:
    logger.info(
        "Result: batches=%d failed_batches=%d embedded_texts=%d rows_updated=%d "
        "api_requests=%d prompt_tokens=%d pending_after=%d elapsed_seconds=%.1f",
        counters.batches,
        counters.failed_batches,
        counters.embedded_texts,
        counters.rows_updated,
        counters.api_requests,
        counters.prompt_tokens,
        counters.pending_after,
        counters.elapsed_seconds,
    )


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m preston.embed_chunks",
        description="Backfill chunk embeddings. A dry run unless --execute is given.",
    )
    parser.add_argument(
        "--execute", action="store_true", help="call the provider and write vectors"
    )
    parser.add_argument(
        "--scope",
        action="append",
        choices=sorted(PUBLIC_SCOPES),
        help="restrict to a scope (repeatable); default is every public scope",
    )
    parser.add_argument("--limit", type=int, help="embed at most this many chunks")
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one backfill and return a process exit code.

    ``0`` it finished (or the dry run completed), ``1`` it failed or stopped
    early, ``2`` another run holds the advisory lock and nothing was done.
    """
    args = _parse(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        report = asyncio.run(
            run_backfill(
                settings,
                scopes=args.scope,
                limit=args.limit,
                execute=args.execute,
                max_cost_usd=args.max_cost_usd,
            )
        )
    except ConcurrentRunError as exc:
        logger.warning("%s", exc.detail)
        return 2
    except BackfillIncomplete as exc:
        logger.error("Embedding backfill stopped: %s", exc.counters.stop_reason)
        log_counters(exc.counters)
        return 1
    except Exception as exc:  # noqa: BLE001
        # Class name only: provider and driver messages can carry secrets.
        logger.error("Embedding backfill failed: %s", type(exc).__name__)
        return 1
    log_summary(report)
    if report.estimated_cost_usd > report.cost_limit_usd:
        logger.warning("Estimated cost is above the limit; --execute would refuse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
