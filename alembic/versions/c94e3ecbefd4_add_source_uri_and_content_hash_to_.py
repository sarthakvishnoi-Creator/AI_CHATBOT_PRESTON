"""add source_uri and content_hash to documents

Revision ID: c94e3ecbefd4
Revises: beb61f002efe
Create Date: 2026-08-27 10:21:09.943417

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c94e3ecbefd4"
down_revision: str | Sequence[str] | None = "beb61f002efe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ingestion identity columns the pipeline keys off of.

    ``source_uri`` is unique so a document can be looked up by its source
    for the idempotency check; ``content_hash`` records the SHA-256 of the
    normalized text last ingested, so unchanged re-ingestion is a no-op.
    """
    op.add_column("documents", sa.Column("source_uri", sa.Text(), nullable=False))
    op.add_column("documents", sa.Column("content_hash", sa.Text(), nullable=False))
    op.create_unique_constraint("uq_documents_source_uri", "documents", ["source_uri"])


def downgrade() -> None:
    """Drop the ingestion identity columns."""
    op.drop_constraint("uq_documents_source_uri", "documents", type_="unique")
    op.drop_column("documents", "content_hash")
    op.drop_column("documents", "source_uri")
