"""saving journal: favourites, the expected day of an anticipated moment

The Saving Journal stays on the shared entries / captured_values tables
(journal_type = 'savouring'); it needs only whether a moment is a favourite,
the day a moment she looks forward to should happen, and an index for counting
a journal's entries day by day. Existing rows: not favourites, no event date.

Revision ID: d4f6a8c0e2b3
Revises: c3e5a7b9d1f2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f6a8c0e2b3"
down_revision: Union[str, Sequence[str], None] = "c3e5a7b9d1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("entries", sa.Column("is_favorite", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("entries", sa.Column("event_date", sa.Date(), nullable=True))
    op.create_index("ix_entries_account_type_date", "entries", ["account_id", "journal_type", "local_date"])


def downgrade() -> None:
    op.drop_index("ix_entries_account_type_date", table_name="entries")
    op.drop_column("entries", "event_date")
    op.drop_column("entries", "is_favorite")
