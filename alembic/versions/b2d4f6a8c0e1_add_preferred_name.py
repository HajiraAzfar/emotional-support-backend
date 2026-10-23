"""add preferred name

What she would like to be called in the app's greeting. Optional, so every
existing account stays as it is.

Revision ID: b2d4f6a8c0e1
Revises: a1c4e7b20f93
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f6a8c0e1"
down_revision: Union[str, Sequence[str], None] = "a1c4e7b20f93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("preferred_name", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "preferred_name")
