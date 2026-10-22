"""add earned badges and weekly reflections

The weekly report ("Your Week in Reflection") needs two things the database
could not hold: which badges an account has earned, and the one line she chose
to carry into the next week.

Both unique constraints are load-bearing. A badge is awarded once however many
times a report is regenerated, and a week has one reflection that she edits
rather than a pile of rows.

Revision ID: a1c4e7b20f93
Revises: d8e4f2a9c613
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c4e7b20f93"
down_revision: Union[str, Sequence[str], None] = "d8e4f2a9c613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "earned_badges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("badge_id", sa.String(length=40), nullable=False),
        sa.Column("earned_at", sa.DateTime(), nullable=False),
        sa.Column("entry_ids", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "badge_id", name="uq_badge_per_account"),
    )
    op.create_index(op.f("ix_earned_badges_account_id"), "earned_badges", ["account_id"])

    op.create_table(
        "weekly_reflections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "week_start", name="uq_reflection_per_week"),
    )
    op.create_index(op.f("ix_weekly_reflections_account_id"), "weekly_reflections", ["account_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_weekly_reflections_account_id"), table_name="weekly_reflections")
    op.drop_table("weekly_reflections")
    op.drop_index(op.f("ix_earned_badges_account_id"), table_name="earned_badges")
    op.drop_table("earned_badges")
