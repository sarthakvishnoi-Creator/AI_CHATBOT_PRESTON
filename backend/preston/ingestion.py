"""Document ingestion — persistence for canonical documents.

Takes a ``CanonicalDocument``, decides whether it is new, unchanged,
changed or reprocessed, and writes the outcome. The hash is the only
oracle: the source carries no usable timestamp, so there is no
timestamp-based alternative.

Framework-free by design: no HTTP client, no FastAPI, and no
repository/service layers. Callers own the transaction — nothing here
calls ``commit``, only ``flush`` where an id is needed before the next
statement. That is what makes one-transaction-per-document possible, and
therefore what guarantees a mid-run crash leaves N complete documents and
zero partial ones.

Extraction, validation and the inventory reconciliation that drives
archival are not here. This module is reached only by a record that has
already been extracted and validated — a broken extraction must never
reach the hash, because hashing a broken extraction and finding it
"changed" is precisely how an empty record overwrites good content.
"""

import enum
from dataclasses import dataclass, replace
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from preston.canonical import (
    Block,
    CanonicalDocument,
    FaqPair,
    ImageRef,
    block_text,
    blocks_to_json,
    hash_document,
)
from preston.models import Document, DocumentChunk

# Current approved chunking parameters (Phase 6). Changing these is a
# content decision, not a code change, so they are not exposed as
# ``ingest_document`` arguments.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

# The character window below still chunks ordinary prose. Bumped 1 -> 2
# when ``build_chunks`` stopped feeding it a flattened document: an
# ``FaqPair`` is now its own chunk and never enters the window at all.
# The full structure-aware chunker — heading tree, token budget, per-chunk
# hashes — is still a later phase; bumping this version is what makes such
# a change a reprocess rather than a corpus-wide false "changed".
#
# Bumped 2 -> 3 when image alt text in :data:`NON_KNOWLEDGE_ALT_TEXT`
# stopped reaching chunk text. No block and no hash changes, so without
# this bump the affected rows would stay UNCHANGED with stale chunks; with
# it, every row is REPROCESSED on its scope's next run.
CHUNKER_VERSION = 3

#: Image alt values that are never knowledge: a placeholder and a
#: call-to-action label, each found verbatim on a published blog image.
#: Matched exactly, after collapsing whitespace and case folding, so the
#: list stays auditable — any other alt text still reaches retrieval.
#: The ``ImageRef`` keeps its alt: the contract (§9) preserves authored
#: alt verbatim and hashes it, so only chunk text is affected here.
NON_KNOWLEDGE_ALT_TEXT: Final = frozenset({"test", "contact us"})


class IngestionOutcome(enum.StrEnum):
    """What ingesting one document did.

    ``REPROCESSED`` is distinct from ``CHANGED`` on purpose. When the
    stored version tuple differs from the current one the two hashes are
    not comparable, so the document is re-derived — but that is our rules
    changing, not the corpus changing, and only one of those should trip a
    change-rate alarm.

    ``ARCHIVED`` has no member here: archival is driven by inventory
    reconciliation across the whole corpus and is gated by the
    mass-archival threshold. Ingesting one document can never archive it.
    """

    NEW = "new"
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    REPROCESSED = "reprocessed"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """The persisted row and what happened to it."""

    document: Document
    outcome: IngestionOutcome


def chunk_text(
    normalized_text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Split normalized text into deterministic, overlapping chunks.

    Pure function of ``normalized_text``, ``size``, and ``overlap``: the
    same input always produces the same chunks in the same order.
    """
    if not normalized_text:
        return []

    step = size - overlap
    chunks: list[str] = []
    start = 0
    length = len(normalized_text)
    while True:
        end = start + size
        chunks.append(normalized_text[start:end])
        if end >= length:
            break
        start += step
    return chunks


def _is_non_knowledge_image(block: Block) -> bool:
    return (
        isinstance(block, ImageRef)
        and " ".join(block.alt.split()).casefold() in NON_KNOWLEDGE_ALT_TEXT
    )


def _chunk_text(block: Block) -> str:
    """Render a block as ``block_text`` does, minus non-knowledge alt text."""
    if _is_non_knowledge_image(block):
        return ""
    if isinstance(block, FaqPair):
        answer = tuple(a for a in block.answer if not _is_non_knowledge_image(a))
        return block_text(replace(block, answer=answer))
    return block_text(block)


def build_chunks(document: CanonicalDocument) -> list[str]:
    """Split a document into retrieval units, in document order.

    The block vocabulary decides the unit, which is why this reads
    ``document.blocks`` rather than a flattened string:

    * an :class:`~preston.canonical.FaqPair` is **one chunk, whole**. It
      never enters the character window and never shares a chunk with a
      neighbouring block, so a question can never be separated from its
      answer, and no ordinary prose can dilute it. A pair longer than
      ``CHUNK_SIZE`` stays one chunk — atomicity outranks the window,
      because half an answer cited as authoritative is worse than an
      oversized chunk.
    * every other block joins the run of ordinary blocks around it, and
      each run is windowed by :func:`chunk_text` exactly as before. An
      FAQ therefore also acts as a run boundary, which is what keeps it
      from being merged into a neighbouring window.
    * an :class:`~preston.canonical.ImageRef` whose alt is in
      :data:`NON_KNOWLEDGE_ALT_TEXT` contributes no text, wherever it sits.

    Pure and deterministic: the same blocks always produce the same
    chunks, with the same content, in the same order.
    """
    chunks: list[str] = []
    run: list[str] = []

    def flush_run() -> None:
        """Window the ordinary blocks accumulated so far, then reset."""
        if run:
            chunks.extend(chunk_text("\n\n".join(run)))
            run.clear()

    for block in document.blocks:
        rendered = _chunk_text(block)
        if isinstance(block, FaqPair):
            flush_run()
            if rendered:
                chunks.append(rendered)
        elif rendered:
            run.append(rendered)
    flush_run()
    return chunks


def _versions_match(row: Document, document: CanonicalDocument) -> bool:
    """Return whether the stored rules are the ones in force now.

    Compared *before* hashes. Two hashes produced by different rule sets
    are not comparable, and a row carrying an older tuple is reprocessed
    rather than guessed at.

    ``chunker_version`` is included even though it plays no part in the
    hash itself: it is the only signal that can tell "this row's chunks
    were built by an older chunker" apart from "nothing changed at all".
    Without it here, a chunker-only version bump would leave every row
    reporting ``UNCHANGED`` and its chunks stale forever.
    """
    provenance = document.provenance
    return (
        row.hash_version == provenance.hash_version
        and row.normalizer_version == provenance.normalizer_version
        and row.extractor_version == provenance.extractor_version
        and row.chunker_version == CHUNKER_VERSION
    )


def _apply(row: Document, document: CanonicalDocument, content_hash: str) -> None:
    """Write a canonical document's content and provenance onto a row."""
    provenance = document.provenance
    row.canonical_uri = document.canonical_uri
    row.source_type = document.source_type
    row.source_ref = document.source_ref
    row.content_type = document.content_type
    row.source_scope = document.source_scope
    row.title = document.title
    row.blocks = list(blocks_to_json(document.blocks))
    row.doc_metadata = dict(document.metadata)
    row.language = document.language
    row.content_hash = content_hash
    row.hash_version = provenance.hash_version
    row.normalizer_version = provenance.normalizer_version
    row.extractor_version = provenance.extractor_version
    row.chunker_version = CHUNKER_VERSION
    row.fetched_at = provenance.retrieved_at
    row.last_seen_at = provenance.retrieved_at
    row.last_run_id = provenance.run_id
    # A successful extraction clears the failure history; the counter
    # measures *consecutive* failures.
    row.consecutive_failure_count = 0
    row.gone_count = 0


async def _rebuild_chunks(
    session: AsyncSession, document: CanonicalDocument, row: Document
) -> None:
    """Replace a document's chunks with a fresh set."""
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == row.id)
    )
    for index, chunk in enumerate(build_chunks(document)):
        session.add(DocumentChunk(document_id=row.id, chunk_index=index, content=chunk))


async def ingest_document(
    session: AsyncSession, document: CanonicalDocument
) -> IngestionResult:
    """Persist one canonical document and report what changed.

    Looks the document up by ``canonical_uri``. When the stored hash and
    the stored version tuple both match, this is a no-op beyond touching
    ``last_seen_at`` and ``last_run_id`` — the blocks and chunks are left
    alone. Otherwise the row is created or updated and its chunks rebuilt.

    A document that reappears after being archived is restored: status
    back to ``active`` and ``gone_count`` cleared. Its content is
    re-derived only if it actually changed.

    Does not commit: the caller owns the transaction.
    """
    content_hash = hash_document(document)
    row = await session.scalar(
        select(Document).where(Document.canonical_uri == document.canonical_uri)
    )

    if row is None:
        row = Document(status="active")
        _apply(row, document, content_hash)
        session.add(row)
        await session.flush()
        await _rebuild_chunks(session, document, row)
        return IngestionResult(row, IngestionOutcome.NEW)

    was_archived = row.status == "archived"
    versions_matched = _versions_match(row, document)
    unchanged = versions_matched and row.content_hash == content_hash

    if unchanged and not was_archived:
        row.last_seen_at = document.provenance.retrieved_at
        row.last_run_id = document.provenance.run_id
        # Seeing the document again is itself a success, even though its
        # content did not move: it is neither consecutively failing nor
        # gone. Without this the counters only ever reset when content
        # happens to change, so a document that failed once and then
        # returned unchanged would carry that failure forever.
        row.consecutive_failure_count = 0
        row.gone_count = 0
        return IngestionResult(row, IngestionOutcome.UNCHANGED)

    _apply(row, document, content_hash)
    if was_archived:
        row.status = "active"
    if not unchanged:
        await _rebuild_chunks(session, document, row)

    if was_archived:
        outcome = IngestionOutcome.RESTORED
    elif versions_matched:
        outcome = IngestionOutcome.CHANGED
    else:
        outcome = IngestionOutcome.REPROCESSED
    return IngestionResult(row, outcome)
