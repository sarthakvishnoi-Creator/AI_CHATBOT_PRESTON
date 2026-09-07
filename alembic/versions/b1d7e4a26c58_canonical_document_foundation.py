"""canonical document foundation

Revision ID: b1d7e4a26c58
Revises: c94e3ecbefd4
Create Date: 2026-09-07 11:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1d7e4a26c58"
down_revision: str | Sequence[str] | None = "c94e3ecbefd4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Columns with no natural default are added with a temporary one so the
# statement is safe on a populated table, then have that default dropped
# so future inserts must supply a real value. Any row written before this
# revision is left with placeholder provenance and an empty block list and
# must be re-ingested; ``documents`` is empty in every environment that
# exists, so this is a safety property rather than a migration of data.
_BACKFILLED: tuple[tuple[str, str], ...] = (
    ("source_type", "'mysql'"),
    ("content_type", "'blog'"),
    ("blocks", "'[]'::jsonb"),
    ("hash_version", "1"),
    ("normalizer_version", "1"),
    ("extractor_version", "1"),
    ("fetched_at", "now()"),
    ("last_seen_at", "now()"),
)


def upgrade() -> None:
    """Establish the canonical document foundation.

    Three things: the ``ingestion_runs`` table that documents point at,
    the rename of ``source_uri`` to ``canonical_uri``, and the identity,
    provenance, lifecycle and versioning columns the canonical model
    needs. The rename is a rename, not a drop and add, so the column's
    data and its unique index survive.
    """
    op.create_table(
        "ingestion_runs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("abort_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("extractor_version", sa.Integer(), nullable=False),
        sa.Column("normalizer_version", sa.Integer(), nullable=False),
        sa.Column("hash_version", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "mode IN ('sync', 'reprocess', 'backfill')",
            name="ck_ingestion_runs_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'aborted')",
            name="ck_ingestion_runs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingestion_runs_started_at", "ingestion_runs", ["started_at"])

    # Identity: the semantics change from "wherever this came from" to
    # "the normalized public URL", and the old meaning moves to
    # ``source_ref``.
    op.alter_column("documents", "source_uri", new_column_name="canonical_uri")
    op.execute(
        "ALTER TABLE documents RENAME CONSTRAINT "
        "uq_documents_source_uri TO uq_documents_canonical_uri"
    )

    op.add_column(
        "documents",
        sa.Column("source_type", sa.Text(), nullable=False, server_default="mysql"),
    )
    op.add_column("documents", sa.Column("source_ref", sa.Text(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("content_type", sa.Text(), nullable=False, server_default="blog"),
    )
    op.add_column(
        "documents",
        sa.Column(
            "blocks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "documents",
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
    )
    op.add_column(
        "documents",
        sa.Column("language", sa.Text(), nullable=False, server_default="en"),
    )
    op.add_column(
        "documents",
        sa.Column("hash_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "documents",
        sa.Column(
            "normalizer_version", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "extractor_version", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column("documents", sa.Column("last_run_id", sa.Uuid(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("gone_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "documents",
        sa.Column(
            "consecutive_failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    for column, _ in _BACKFILLED:
        op.alter_column("documents", column, server_default=None)

    op.create_check_constraint(
        "ck_documents_source_type",
        "documents",
        "source_type IN ('mysql', 'api', 'web')",
    )
    op.create_check_constraint(
        "ck_documents_content_type",
        "documents",
        "content_type IN ('blog', 'service', 'faq', 'resource', "
        "'corporate', 'location', 'accreditation')",
    )
    op.create_check_constraint(
        "ck_documents_status", "documents", "status IN ('active', 'archived')"
    )
    op.create_foreign_key(
        "fk_documents_last_run_id",
        "documents",
        "ingestion_runs",
        ["last_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"])
    op.create_index(
        "ix_documents_last_seen_at_active",
        "documents",
        ["last_seen_at"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_documents_metadata", "documents", ["metadata"], postgresql_using="gin"
    )


def downgrade() -> None:
    """Return ``documents`` to its pre-canonical shape.

    Reversible in structure, not in knowledge: the blocks, provenance and
    lifecycle state this revision introduced are dropped with their
    columns. The identity column keeps its data through the rename back.
    """
    op.drop_index("ix_documents_metadata", table_name="documents")
    op.drop_index("ix_documents_last_seen_at_active", table_name="documents")
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.drop_constraint("fk_documents_last_run_id", "documents", type_="foreignkey")
    op.drop_constraint("ck_documents_status", "documents", type_="check")
    op.drop_constraint("ck_documents_content_type", "documents", type_="check")
    op.drop_constraint("ck_documents_source_type", "documents", type_="check")

    for column in (
        "consecutive_failure_count",
        "gone_count",
        "last_run_id",
        "last_seen_at",
        "fetched_at",
        "extractor_version",
        "normalizer_version",
        "hash_version",
        "language",
        "status",
        "metadata",
        "blocks",
        "content_type",
        "source_ref",
        "source_type",
    ):
        op.drop_column("documents", column)

    op.execute(
        "ALTER TABLE documents RENAME CONSTRAINT "
        "uq_documents_canonical_uri TO uq_documents_source_uri"
    )
    op.alter_column("documents", "canonical_uri", new_column_name="source_uri")

    op.drop_index("ix_ingestion_runs_started_at", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
