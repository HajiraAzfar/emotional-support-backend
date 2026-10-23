"""check-in slots, source entry, crisis acknowledgement

Three check-ins a day (morning, afternoon, evening), one per slot per day; the
check-in a journal was opened from; and when crisis content was acknowledged.
Every column is nullable, so every existing row stays as it is.

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e5a7b9d1f2"
down_revision: Union[str, Sequence[str], None] = "b2d4f6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("entries", sa.Column("slot", sa.String(length=12), nullable=True))
    op.add_column("entries", sa.Column("local_date", sa.Date(), nullable=True))
    op.add_column("entries", sa.Column("tz_offset", sa.Integer(), nullable=True))
    op.add_column("entries", sa.Column("timezone", sa.String(length=64), nullable=True))
    op.add_column("entries", sa.Column("source_entry_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_entries_source_entry_id", "entries", "entries", ["source_entry_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(
        "uq_entries_checkin_slot", "entries", ["account_id", "local_date", "slot"], unique=True,
        postgresql_where=sa.text("slot IS NOT NULL"),
    )
    op.add_column("crisis_events", sa.Column("acknowledged_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("crisis_events", "acknowledged_at")
    op.drop_index("uq_entries_checkin_slot", table_name="entries")
    op.drop_constraint("fk_entries_source_entry_id", "entries", type_="foreignkey")
    op.drop_column("entries", "source_entry_id")
    op.drop_column("entries", "timezone")
    op.drop_column("entries", "tz_offset")
    op.drop_column("entries", "local_date")
    op.drop_column("entries", "slot")
