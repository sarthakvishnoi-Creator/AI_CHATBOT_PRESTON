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
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from preston.canonical import (
    CanonicalDocument,
    blocks_to_json,
    content_text,
    hash_document,
)
from preston.models import Document, DocumentChunk

# Current approved chunking parameters (Phase 6). Changing these is a
# content decision, not a code change, so they are not exposed as
# ``ingest_document`` arguments.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

# The character-window chunker below. The structure-aware chunker that
# replaces it — heading tree, token budget, per-chunk hashes — is a later
# phase; bumping this version is what makes that change a reprocess rather
# than a corpus-wide false "changed".
CHUNKER_VERSION = 1


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


def _versions_match(row: Document, document: CanonicalDocument) -> bool:
    """Return whether the stored rules are the ones in force now.

    Compared *before* hashes. Two hashes produced by different rule sets
    are not comparable, and a row carrying an older tuple is reprocessed
    rather than guessed at.
    """
    provenance = document.provenance
    return (
        row.hash_version == provenance.hash_version
        and row.normalizer_version == provenance.normalizer_version
        and row.extractor_version == provenance.extractor_version
    )


def _apply(row: Document, document: CanonicalDocument, content_hash: str) -> None:
    """Write a canonical document's content and provenance onto a row."""
    provenance = document.provenance
    row.canonical_uri = document.canonical_uri
    row.source_type = document.source_type
    row.source_ref = document.source_ref
    row.content_type = document.content_type
    row.title = document.title
    row.blocks = list(blocks_to_json(document.blocks))
    row.doc_metadata = dict(document.metadata)
    row.language = document.language
    row.content_hash = content_hash
    row.hash_version = provenance.hash_version
    row.normalizer_version = provenance.normalizer_version
    row.extractor_version = provenance.extractor_version
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
    for index, chunk in enumerate(chunk_text(content_text(document))):
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
