"""add chunker_version and source_scope to documents

Revision ID: 8fc561d8753e
Revises: b1d7e4a26c58
Create Date: 2026-09-16 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8fc561d8753e"
down_revision: str | Sequence[str] | None = "b1d7e4a26c58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both columns are added with a temporary default so the statement is safe
# on the populated ``documents`` table, then have that default dropped so
# future writes (``ingestion.py``'s ``_apply``) must supply a real value.
# The backfill values are not placeholders: every row in every environment
# this migration has run against is a Blog document chunked under
# ``CHUNKER_VERSION`` 2 at the time of writing (verified against the live
# corpus, Phase 6.5-A.4/A.5) — a chunker rebuild is therefore not required
# by this migration, only by a future ``CHUNKER_VERSION`` bump.
_CHUNKER_VERSION_BACKFILL = "2"
_SOURCE_SCOPE_BACKFILL = "blog"


def upgrade() -> None:
    """Add per-document chunker-version tracking and the reconciliation scope.

    Two independent, additive columns:

    * ``chunker_version`` lets ``ingest_document`` detect a chunker-only
      rule change, which the run-level ``ingestion_runs.chunker_version``
      cannot — that one is stored per run, never per document.
    * ``source_scope`` is the owning adapter's reconciliation boundary, so
      ``reconcile_missing`` can filter to one adapter's documents instead
      of the whole ``documents`` table.
    """
    op.add_column(
        "documents",
        sa.Column(
            "chunker_version",
            sa.Integer(),
            nullable=False,
            server_default=_CHUNKER_VERSION_BACKFILL,
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "source_scope",
            sa.Text(),
            nullable=False,
            server_default=_SOURCE_SCOPE_BACKFILL,
        ),
    )
    op.alter_column("documents", "chunker_version", server_default=None)
    op.alter_column("documents", "source_scope", server_default=None)

    op.create_index(
        "ix_documents_source_scope_active",
        "documents",
        ["source_scope"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    """Drop both columns and the scope index."""
    op.drop_index("ix_documents_source_scope_active", table_name="documents")
    op.drop_column("documents", "source_scope")
    op.drop_column("documents", "chunker_version")
