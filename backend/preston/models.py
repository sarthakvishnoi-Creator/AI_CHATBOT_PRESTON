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

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
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


class Document(Base):
    """A source document that retrieval will later draw on."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=func.gen_random_uuid()
    )
    title: Mapped[str] = mapped_column(Text)
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
