"""enable pgvector extension

Revision ID: 0d2a9b650819
Revises:
Create Date: 2026-08-26 07:03:31.809906

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d2a9b650819"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable the pgvector extension the server image already ships."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Remove the pgvector extension this migration owns.

    No cascade: at this phase nothing depends on it, so a plain drop
    fails loudly instead of silently taking dependents down with it.
    """
    op.execute("DROP EXTENSION IF EXISTS vector")
