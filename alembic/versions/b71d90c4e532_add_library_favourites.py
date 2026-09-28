"""add library favourites

SRS 4.11: the articles she saved from the learning library.

Revision ID: b71d90c4e532
Revises: e5a17c2b90d4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b71d90c4e532"
down_revision: Union[str, Sequence[str], None] = "e5a17c2b90d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "library_favourites",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "slug", name="uq_library_favourite"),
    )
    op.create_index(op.f("ix_library_favourites_account_id"), "library_favourites", ["account_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_library_favourites_account_id"), table_name="library_favourites")
    op.drop_table("library_favourites")
