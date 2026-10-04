"""Application ORM models.

One module, not a package: the schema is small enough to read in one
sitting. The one vector column lives on ``document_chunks`` beside the
text it was computed from; its dimension is fixed by the approved
embedding model (Phase 7, :data:`EMBEDDING_DIMENSIONS`).

Relationships are deliberately absent. Foreign keys and ``ON DELETE
CASCADE`` enforce integrity in the database; ORM-level ``relationship()``
adds loading behaviour nothing queries yet, and is a one-line addition
when something does.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
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

#: The approved embedding model's output dimension (OpenAI
#: ``text-embedding-3-large``). A schema commitment, not a setting: changing
#: it is a migration. Stored as ``halfvec`` because pgvector indexes plain
#: ``vector`` only up to 2,000 dimensions.
EMBEDDING_DIMENSIONS = 3072


class Conversation(Base):
    """A chat thread whose history outlives a single request.

    **Ownership (Phase 9B).** The visitor holds a random token issued once by
    the application; only its SHA-256 hex digest is stored here. Nothing in
    this row grants access on its own, and a conversation id — which appears
    in logs and admin views — is never enough to open the thread.

    A conversation has no status column: it is active until ``updated_at`` is
    older than the configured inactivity window, and its support state lives
    in ``support_requests``.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "char_length(visitor_token_hash) = 64",
            name="ck_conversations_visitor_token_hash",
        ),
        # Expiry checks, the retention purge and admin lists by recency.
        Index("ix_conversations_updated_at", "updated_at"),
    )

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
    visitor_token_hash: Mapped[str] = mapped_column(Text)


class Message(Base):
    """One message within a conversation, in ``sequence`` order.

    A user message is immutable once written. An assistant message is
    inserted as ``in_progress`` when its turn begins — so tool calls can
    reference it — and completed exactly once when the turn ends. Only
    assistant messages carry a ``status``; usage, latency and model fields
    describe the turn that produced them.

    Messages hold no personal contact fields: those exist only on
    ``support_requests``.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # A CHECK constraint rather than a PostgreSQL ENUM type: adding a
        # role later is then an ordinary migration, not an ``ALTER TYPE``.
        CheckConstraint(
            "role IN ('system', 'user', 'assistant')",
            name="ck_messages_role",
        ),
        CheckConstraint("sequence >= 1", name="ck_messages_sequence"),
        CheckConstraint(
            "status IS NULL OR status IN "
            "('in_progress', 'answered', 'uncited', 'no_evidence', 'error', "
            "'cancelled')",
            name="ck_messages_status",
        ),
        # An assistant message always has a status; no other message does.
        CheckConstraint(
            "(role = 'assistant') = (status IS NOT NULL)",
            name="ck_messages_status_assistant",
        ),
        # Ordering, and the backstop against two turns taking one position.
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_messages_conversation_id_sequence"
        ),
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
    sequence: Mapped[int]
    status: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    invalid_citations: Mapped[int | None]
    # A fixed vocabulary chosen by the application, never exception text.
    error_category: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    """One tool call the model requested during a turn — an audit record.

    ``tool_name`` is what the model *asked for* and is deliberately not
    limited to the known tools: a refused call such as ``run_sql`` must be
    recordable. Recording grants nothing; only ``agent.run_tool`` decides what
    runs. No tool output is stored here — evidence is referenced in
    ``message_evidence``.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "position", name="uq_tool_calls_message_id_position"
        ),
        # The target of message_evidence's composite foreign key: evidence can
        # only point at a tool call of the same assistant message.
        UniqueConstraint("id", "message_id", name="uq_tool_calls_id_message_id"),
        CheckConstraint("position >= 0", name="ck_tool_calls_position"),
        CheckConstraint(
            "char_length(tool_name) BETWEEN 1 AND 100", name="ck_tool_calls_tool_name"
        ),
        CheckConstraint(
            "tool_name = 'search_knowledge' OR query IS NULL",
            name="ck_tool_calls_query",
        ),
        CheckConstraint(
            "status IN ('ok', 'empty', 'error', 'timeout')", name="ck_tool_calls_status"
        ),
        CheckConstraint(
            "result_count >= 0 AND duration_ms >= 0", name="ck_tool_calls_counts"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    position: Mapped[int]
    tool_name: Mapped[str] = mapped_column(Text)
    query: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    result_count: Mapped[int]
    duration_ms: Mapped[int]
    error_category: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MessageEvidence(Base):
    """A reference to one piece of evidence a turn retrieved.

    References, not copies: the text stays in ``document_chunks`` and is found
    again by ``chunk_content_hash``. ``label`` (``S1``…) is present exactly
    when the evidence was supplied to the model; evidence dropped by the
    source budget is still recorded, unlabelled. Only supplied evidence can be
    cited.
    """

    __tablename__ = "message_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tool_call_id", "message_id"],
            ["tool_calls.id", "tool_calls.message_id"],
            ondelete="CASCADE",
            name="fk_message_evidence_tool_call",
        ),
        UniqueConstraint(
            "message_id", "label", name="uq_message_evidence_message_id_label"
        ),
        UniqueConstraint(
            "message_id",
            "chunk_content_hash",
            "canonical_uri",
            name="uq_message_evidence_message_id_chunk",
        ),
        CheckConstraint(
            "supplied = (label IS NOT NULL)", name="ck_message_evidence_supplied_label"
        ),
        CheckConstraint("NOT cited OR supplied", name="ck_message_evidence_cited"),
        CheckConstraint(
            "retrieval_method IN ('vector', 'structured')",
            name="ck_message_evidence_retrieval_method",
        ),
        Index("ix_message_evidence_chunk_content_hash", "chunk_content_hash"),
        # Serves the cascade from tool_calls; without it every deleted tool
        # call would scan this table.
        Index("ix_message_evidence_tool_call_id", "tool_call_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    tool_call_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    label: Mapped[str | None] = mapped_column(Text)
    chunk_content_hash: Mapped[str] = mapped_column(Text)
    canonical_uri: Mapped[str] = mapped_column(Text)
    chunk_index: Mapped[int]
    document_content_hash: Mapped[str | None] = mapped_column(Text)
    retrieval_method: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Double)
    supplied: Mapped[bool]
    cited: Mapped[bool]


class SupportRequest(Base):
    """A visitor's explicit request for a person (decision A5).

    The only place contact details are stored: name, email and company are
    required, a reason is optional. They are collected on the visitor's
    explicit action, never logged, and never shown to the model. One open
    request per conversation.
    """

    __tablename__ = "support_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'acknowledged', 'resolved')",
            name="ck_support_requests_status",
        ),
        CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 200",
            name="ck_support_requests_name",
        ),
        CheckConstraint(
            "company = btrim(company) AND char_length(company) BETWEEN 1 AND 200",
            name="ck_support_requests_company",
        ),
        # Shape only (one @, a dot in the domain, no whitespace); whether the
        # address exists is not, and cannot be, checked here.
        CheckConstraint(
            "char_length(email) <= 254 AND "
            "email ~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$'",
            name="ck_support_requests_email",
        ),
        CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 1000",
            name="ck_support_requests_reason",
        ),
        CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)",
            name="ck_support_requests_resolved_at",
        ),
        Index(
            "uq_support_requests_open_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status <> 'resolved'"),
        ),
        Index("ix_support_requests_status_created_at", "status", "created_at"),
        # Serves the cascade from conversations and per-conversation lookups
        # across every status (the partial index covers open requests only).
        Index("ix_support_requests_conversation_id", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'requested'"))
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    handled_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
        CheckConstraint(
            "mode = 'backfill' OR extractor_version IS NOT NULL",
            name="ck_ingestion_runs_extractor_version",
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
    # Null only for an embedding backfill, which runs no extractor
    # (``ck_ingestion_runs_extractor_version``); every other run names one.
    extractor_version: Mapped[int | None]
    normalizer_version: Mapped[int]
    hash_version: Mapped[int]
    chunker_version: Mapped[int]
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

    ``source_scope`` is the owning adapter's reconciliation boundary —
    finer than ``source_type``/``content_type``, which future adapters
    may share. Missing-reconciliation filters on it so one adapter's run
    can never flag another adapter's documents as gone (Phase 6.5-A.5).

    ``chunker_version`` is tracked per document, not only per run: without
    it, a chunker-only version bump could never be told apart from "no
    version changed at all", and ``ingest_document`` would leave stale
    chunks in place with nothing to detect it.
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
        # Reconciliation's other query: every active document *in one
        # adapter's scope*. Serves the same completeness-gated check as
        # the index above, scoped instead of ordered.
        Index(
            "ix_documents_source_scope_active",
            "source_scope",
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
    # The owning adapter's reconciliation boundary, e.g. "blog" or "grc" —
    # not a content taxonomy value, and never CHECK-constrained to a fixed
    # vocabulary, because each new adapter introduces its own.
    source_scope: Mapped[str] = mapped_column(Text)
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
    # Tracked per document (unlike ingestion_runs.chunker_version, which is
    # per run): only this lets ingest_document tell "this row's chunks were
    # built by an older chunker" apart from "nothing changed".
    chunker_version: Mapped[int]
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

    ``content_hash`` is ``hash_content(content)`` — the SHA-256 of the
    exact text that is embedded. It, not the row id, is the chunk's
    semantic identity: ids are regenerated on every rebuild, while the
    hash survives unchanged text. ``embedding`` is derived data and may be
    absent; ``embedding_model`` names what produced it, and the two are
    null together or set together.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        # Ordering within a document is part of the data, not an accident
        # of insertion order, so duplicates are a bug worth rejecting.
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_id_index"
        ),
        # A vector without its model, or a model without its vector, could
        # be compared against vectors from a different model.
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)",
            name="ck_document_chunks_embedding_model",
        ),
        Index("ix_document_chunks_content_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
