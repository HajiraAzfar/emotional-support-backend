"""thought journal: when a finished record was edited later

A thought record can be changed freely for a day after it is finished; an
edit after that is marked with edited_at. The rest of what the journal needs
(completed_at, is_favorite, source_entry_id) is already on entries.

Revision ID: e5a7c9e1f3b4
Revises: d4f6a8c0e2b3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a7c9e1f3b4"
down_revision: Union[str, Sequence[str], None] = "d4f6a8c0e2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("entries", sa.Column("edited_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("entries", "edited_at")
