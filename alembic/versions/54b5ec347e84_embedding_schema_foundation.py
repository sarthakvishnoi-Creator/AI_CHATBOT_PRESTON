"""embedding schema foundation

Revision ID: 54b5ec347e84
Revises: 8fc561d8753e
Create Date: 2026-09-30 12:00:00.000000

"""

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

# revision identifiers, used by Alembic.
revision: str = "54b5ec347e84"
down_revision: str | Sequence[str] | None = "8fc561d8753e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied, not imported: a migration must keep meaning what it meant when it
# was written, whatever ``preston.models`` says later. OpenAI
# ``text-embedding-3-large`` at its full 3,072 dimensions, stored as
# ``halfvec`` because pgvector indexes plain ``vector`` only to 2,000.
_EMBEDDING_DIMENSIONS = 3072

# ``preston.canonical.hash_content`` in SQL: SHA-256 of the text's UTF-8
# bytes, as lowercase hex. ``convert_to(..., 'UTF8')`` yields those bytes
# untransformed; a ``text::bytea`` cast would not — it interprets
# backslash escapes. ``sha256()`` is core PostgreSQL (11+), so no
# extension is needed.
_SQL_CONTENT_HASH = "encode(sha256(convert_to(content, 'UTF8')), 'hex')"


def _verify_content_hashes() -> None:
    """Recompute every chunk's hash in Python and refuse any disagreement.

    Runs against the real rows of whichever database is being migrated,
    inside the migration's transaction, so a mismatch rolls the whole
    revision back. ``hashlib`` here is exactly what ``hash_content`` does;
    it is inlined for the same reason the dimension is.
    """
    rows = op.get_bind().execute(
        sa.text("SELECT id, content, content_hash FROM document_chunks")
    )
    mismatched = [
        row.id
        for row in rows
        if row.content_hash != hashlib.sha256(row.content.encode("utf-8")).hexdigest()
    ]
    if mismatched:
        raise RuntimeError(
            f"content_hash disagrees with SHA-256 of content for "
            f"{len(mismatched)} chunk(s); migration rolled back."
        )


def upgrade() -> None:
    """Prepare ``document_chunks`` for embeddings, and runs for backfills.

    Additive only. Existing chunks keep their ids, positions and text; the
    one write to them fills the new ``content_hash`` column. No vector is
    written and no vector index is built — search starts exact.
    """
    op.add_column("document_chunks", sa.Column("content_hash", sa.Text()))
    op.execute(f"UPDATE document_chunks SET content_hash = {_SQL_CONTENT_HASH}")
    _verify_content_hashes()
    op.alter_column("document_chunks", "content_hash", nullable=False)
    op.create_index(
        "ix_document_chunks_content_hash", "document_chunks", ["content_hash"]
    )

    op.add_column(
        "document_chunks",
        sa.Column("embedding", HALFVEC(_EMBEDDING_DIMENSIONS), nullable=True),
    )
    op.add_column(
        "document_chunks", sa.Column("embedding_model", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "ck_document_chunks_embedding_model",
        "document_chunks",
        "(embedding IS NULL) = (embedding_model IS NULL)",
    )

    # An embedding backfill runs no extractor, so it has no version to
    # record. Every other mode still must.
    op.alter_column("ingestion_runs", "extractor_version", nullable=True)
    op.create_check_constraint(
        "ck_ingestion_runs_extractor_version",
        "ingestion_runs",
        "mode = 'backfill' OR extractor_version IS NOT NULL",
    )


def downgrade() -> None:
    """Drop the embedding columns and restore the extractor requirement.

    Irreversible in data, not structure: stored vectors are dropped with
    their column. Restoring ``extractor_version NOT NULL`` fails loudly if a
    backfill run row exists, rather than deleting run history.
    """
    op.drop_constraint(
        "ck_ingestion_runs_extractor_version", "ingestion_runs", type_="check"
    )
    op.alter_column("ingestion_runs", "extractor_version", nullable=False)

    op.drop_constraint(
        "ck_document_chunks_embedding_model", "document_chunks", type_="check"
    )
    op.drop_column("document_chunks", "embedding_model")
    op.drop_column("document_chunks", "embedding")
    op.drop_index("ix_document_chunks_content_hash", table_name="document_chunks")
    op.drop_column("document_chunks", "content_hash")
