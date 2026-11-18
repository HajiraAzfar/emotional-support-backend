"""accounts.app_lock_pin: the app lock follows the account

The phone's salted PIN hash ({"salt", "hash"}, hex), so a PIN set on one
phone works on any phone the account signs in on. Null means no app lock.
The table's row-level security already covers the new column.

Revision ID: a8d4c2e6f1b3
Revises: f6b8d0f2a4c5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4c2e6f1b3"
down_revision: Union[str, Sequence[str], None] = "f6b8d0f2a4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("app_lock_pin", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "app_lock_pin")
