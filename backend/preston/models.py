"""Application ORM models.

One module, not a package: the schema is small enough to read in one
sitting. Tables carry no vector column yet — pgvector is enabled in the
database, but the embedding model (and therefore the vector dimension)
has not been approved, so that column belongs to the phase that picks one.

Relationships are deliberately absent. Foreign keys and ``ON DELETE
CASCADE`` enforce integrity in the database; ORM-level ``relationship()``
adds loading behaviour nothing queries yet, and is a one-line addition
when something does.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from preston.core.db import Base


class Conversation(Base):
    """A chat thread whose history outlives a single request."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Bumped when a turn is appended, so conversation lists can sort by
    # recent activity without aggregating over messages.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    """One turn within a conversation.

    Immutable once written, so it carries no ``updated_at``.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # A CHECK constraint rather than a PostgreSQL ENUM type: adding a
        # role later is then an ordinary migration, not an ``ALTER TYPE``.
        CheckConstraint(
            "role IN ('system', 'user', 'assistant')",
            name="ck_messages_role",
        ),
        # Threads are read in written order; this serves that one query.
        Index(
            "ix_messages_conversation_id_created_at", "conversation_id", "created_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestionRun(Base):
    """One execution of the ingestion pipeline.

    Not a log line: the mass-archival gate and every health metric query
    this table. ``counts`` holds per-adapter figures rather than the table
    carrying a ``source_type`` column, because one run spans adapters.

    The version quartet is recorded per run so that an operator can tell a
    corpus-wide change from a change to our own rules — without it, the two
    are indistinguishable and a compromised extractor looks exactly like a
    site redesign.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('sync', 'reprocess', 'backfill')",
            name="ck_ingestion_runs_mode",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'aborted')",
            name="ck_ingestion_runs_status",
        ),
        # Health queries and the next run's stale-``running`` sweep both
        # order by start time; a btree serves either direction.
        Index("ix_ingestion_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    mode: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    abort_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    extractor_version: Mapped[int]
    normalizer_version: Mapped[int]
    hash_version: Mapped[int]
    chunker_version: Mapped[int]
    # No embedding model is approved, so a run cannot yet name one.
    embedding_model: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    """One canonical public document, and the last known-good copy of it.

    Identity is ``canonical_uri`` — the normalized public URL produced by
    one tested function, not "wherever this came from". The native source
    identity moved to ``source_ref``.

    ``blocks`` is the authoritative content; ``metadata`` never is and is
    never hashed. Storing the canonical blocks makes a chunker or
    tokenizer change a local reprocess with no source round-trip, which
    matters when source access is itself an open approval item.

    There is deliberately no ``failed`` status. A document whose
    re-extraction failed has not failed — it still holds correct content
    worth serving — so failures increment a counter and never touch
    ``status``. That is what makes "failure never destroys knowledge"
    enforceable rather than aspirational.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('mysql', 'api', 'web')",
            name="ck_documents_source_type",
        ),
        CheckConstraint(
            "content_type IN ('blog', 'service', 'faq', 'resource', "
            "'corporate', 'location', 'accreditation')",
            name="ck_documents_content_type",
        ),
        # Two persisted states only. Archiving retains blocks and chunks,
        # so removal is reversible and unattended archival is safe.
        CheckConstraint("status IN ('active', 'archived')", name="ck_documents_status"),
        # Cross-URL duplicate detection: two canonical URIs sharing a hash
        # are the same content published twice.
        Index("ix_documents_content_hash", "content_hash"),
        # Inventory reconciliation only ever asks this of live documents.
        Index(
            "ix_documents_last_seen_at_active",
            "last_seen_at",
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_documents_metadata", "metadata", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    # The identity anchor and the citation link. Unique so re-ingestion of
    # the same page updates the existing row instead of duplicating it.
    canonical_uri: Mapped[str] = mapped_column(Text, unique=True)
    source_type: Mapped[str] = mapped_column(Text)
    # Native identity in that source, e.g. "subone_newblogs#1421". Null
    # where a document is composed from more than one source record.
    source_ref: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list[Any]] = mapped_column(JSONB)
    # "metadata" is taken on the declarative base, so the attribute is
    # renamed while the column keeps the name the architecture gives it.
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default="{}"
    )
    status: Mapped[str] = mapped_column(Text, server_default="active")
    language: Mapped[str] = mapped_column(Text, server_default="en")
    # SHA-256 of the hash-normalized canonical serialization. The only
    # oracle of change: the source carries no usable timestamp.
    content_hash: Mapped[str] = mapped_column(Text)
    hash_version: Mapped[int]
    normalizer_version: Mapped[int]
    extractor_version: Mapped[int]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Kept when its run is deleted: losing the run history must not lose
    # the document.
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )
    gone_count: Mapped[int] = mapped_column(server_default="0")
    consecutive_failure_count: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocumentChunk(Base):
    """A contiguous slice of a document, in document order.

    Immutable once written: re-chunking replaces a document's rows rather
    than editing them, so there is no ``updated_at``.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        # Ordering within a document is part of the data, not an accident
        # of insertion order, so duplicates are a bug worth rejecting.
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_id_index"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
