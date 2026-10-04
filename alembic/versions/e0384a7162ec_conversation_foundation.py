"""conversation foundation

Revision ID: e0384a7162ec
Revises: 54b5ec347e84
Create Date: 2026-10-03 10:00:00.000000

Phase 9B: conversation ownership and turn metadata on the existing
``conversations`` and ``messages`` tables, and three new tables —
``tool_calls``, ``message_evidence`` and ``support_requests``. Touches no
knowledge-base table: documents, chunks, embeddings and ingestion runs are
neither read nor written.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e0384a7162ec"
down_revision: str | Sequence[str] | None = "54b5ec347e84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied, not imported: a migration must keep meaning what it meant when it
# was written, whatever ``preston.models`` says later.
_MESSAGE_STATUSES = (
    "'in_progress', 'answered', 'uncited', 'no_evidence', 'error', 'cancelled'"
)
_TOOL_STATUSES = "'ok', 'empty', 'error', 'timeout'"
_SUPPORT_STATUSES = "'requested', 'acknowledged', 'resolved'"
_EMAIL_SHAPE = "^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$"


def _refuse_if_conversations_exist() -> None:
    """Refuse to migrate unless both conversation tables are empty.

    The new ``NOT NULL`` columns have no defaults — there is no honest value
    for an existing conversation's ownership token or a message's sequence.
    Production holds no conversations, so this is a proof, not a hope. Raised
    inside the migration's transaction, it rolls the whole revision back;
    nothing is deleted to make it pass.
    """
    bind = op.get_bind()
    for table in ("conversations", "messages"):
        rows = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if rows:
            raise RuntimeError(
                f"{table} holds {rows} row(s); the conversation foundation "
                "migration only runs on empty conversation tables."
            )


def upgrade() -> None:
    """Add ownership and turn metadata, then the three audit/support tables."""
    _refuse_if_conversations_exist()

    # conversations — ownership (only a SHA-256 hex digest is stored).
    op.add_column(
        "conversations", sa.Column("visitor_token_hash", sa.Text(), nullable=False)
    )
    op.create_check_constraint(
        "ck_conversations_visitor_token_hash",
        "conversations",
        "char_length(visitor_token_hash) = 64",
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])

    # messages — order, assistant status and turn metadata.
    op.add_column("messages", sa.Column("sequence", sa.Integer(), nullable=False))
    for name in ("status", "request_id", "model", "error_category"):
        op.add_column("messages", sa.Column(name, sa.Text(), nullable=True))
    for name in ("input_tokens", "output_tokens", "latency_ms", "invalid_citations"):
        op.add_column("messages", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint("ck_messages_sequence", "messages", "sequence >= 1")
    op.create_check_constraint(
        "ck_messages_status",
        "messages",
        f"status IS NULL OR status IN ({_MESSAGE_STATUSES})",
    )
    op.create_check_constraint(
        "ck_messages_status_assistant",
        "messages",
        "(role = 'assistant') = (status IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_messages_conversation_id_sequence",
        "messages",
        ["conversation_id", "sequence"],
    )

    op.create_table(
        "tool_calls",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "message_id", "position", name="uq_tool_calls_message_id_position"
        ),
        sa.UniqueConstraint("id", "message_id", name="uq_tool_calls_id_message_id"),
        sa.CheckConstraint("position >= 0", name="ck_tool_calls_position"),
        # A length bound, deliberately not a list of names: a refused call
        # (``run_sql``) must be recordable as an audit event.
        sa.CheckConstraint(
            "char_length(tool_name) BETWEEN 1 AND 100", name="ck_tool_calls_tool_name"
        ),
        sa.CheckConstraint(
            "tool_name = 'search_knowledge' OR query IS NULL",
            name="ck_tool_calls_query",
        ),
        sa.CheckConstraint(
            f"status IN ({_TOOL_STATUSES})", name="ck_tool_calls_status"
        ),
        sa.CheckConstraint(
            "result_count >= 0 AND duration_ms >= 0", name="ck_tool_calls_counts"
        ),
    )

    op.create_table(
        "message_evidence",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("chunk_content_hash", sa.Text(), nullable=False),
        sa.Column("canonical_uri", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("document_content_hash", sa.Text(), nullable=True),
        sa.Column("retrieval_method", sa.Text(), nullable=False),
        sa.Column("score", sa.Double(), nullable=True),
        sa.Column("supplied", sa.Boolean(), nullable=False),
        sa.Column("cited", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        # Composite: evidence can only point at a tool call of the same message.
        sa.ForeignKeyConstraint(
            ["tool_call_id", "message_id"],
            ["tool_calls.id", "tool_calls.message_id"],
            ondelete="CASCADE",
            name="fk_message_evidence_tool_call",
        ),
        sa.UniqueConstraint(
            "message_id", "label", name="uq_message_evidence_message_id_label"
        ),
        sa.UniqueConstraint(
            "message_id",
            "chunk_content_hash",
            "canonical_uri",
            name="uq_message_evidence_message_id_chunk",
        ),
        sa.CheckConstraint(
            "supplied = (label IS NOT NULL)", name="ck_message_evidence_supplied_label"
        ),
        sa.CheckConstraint("NOT cited OR supplied", name="ck_message_evidence_cited"),
        sa.CheckConstraint(
            "retrieval_method IN ('vector', 'structured')",
            name="ck_message_evidence_retrieval_method",
        ),
    )
    op.create_index(
        "ix_message_evidence_chunk_content_hash",
        "message_evidence",
        ["chunk_content_hash"],
    )
    op.create_index(
        "ix_message_evidence_tool_call_id", "message_evidence", ["tool_call_id"]
    )

    op.create_table(
        "support_requests",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'requested'"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("handled_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            f"status IN ({_SUPPORT_STATUSES})", name="ck_support_requests_status"
        ),
        sa.CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 200",
            name="ck_support_requests_name",
        ),
        sa.CheckConstraint(
            "company = btrim(company) AND char_length(company) BETWEEN 1 AND 200",
            name="ck_support_requests_company",
        ),
        sa.CheckConstraint(
            f"char_length(email) <= 254 AND email ~ '{_EMAIL_SHAPE}'",
            name="ck_support_requests_email",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 1000",
            name="ck_support_requests_reason",
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)",
            name="ck_support_requests_resolved_at",
        ),
    )
    op.create_index(
        "uq_support_requests_open_conversation",
        "support_requests",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'resolved'"),
    )
    op.create_index(
        "ix_support_requests_status_created_at",
        "support_requests",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_support_requests_conversation_id", "support_requests", ["conversation_id"]
    )


def downgrade() -> None:
    """Drop the three tables and the added columns, restoring the 7A schema.

    Irreversible in data, not structure: any stored conversations' tokens,
    message metadata, tool-call and evidence records, and support requests
    (with their contact details) are dropped with their tables and columns.
    Nothing in the knowledge base is touched.
    """
    op.drop_table("support_requests")
    op.drop_table("message_evidence")
    op.drop_table("tool_calls")

    op.drop_constraint(
        "uq_messages_conversation_id_sequence", "messages", type_="unique"
    )
    for name in (
        "ck_messages_status_assistant",
        "ck_messages_status",
        "ck_messages_sequence",
    ):
        op.drop_constraint(name, "messages", type_="check")
    for column in (
        "completed_at",
        "invalid_citations",
        "latency_ms",
        "output_tokens",
        "input_tokens",
        "error_category",
        "model",
        "request_id",
        "status",
        "sequence",
    ):
        op.drop_column("messages", column)

    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_constraint(
        "ck_conversations_visitor_token_hash", "conversations", type_="check"
    )
    op.drop_column("conversations", "visitor_token_hash")
