"""exposure journal: fear ladders and their steps

Two tables for the Exposure Journal's fear ladders; the exposures themselves
stay entries (journal_type = 'exposure') with captured_values, and every
column they need (source_entry_id, is_favorite, completed_at, edited_at) is
already on entries.

Row-level security is switched on for both tables. The backend connects as the
tables' owner and checks ownership in every query itself; RLS keeps Supabase's
own API roles from reading either table, and on Supabase a policy lets a
signed-in user see only their own ladders, and a step only through its ladder.

Revision ID: f6b8d0f2a4c5
Revises: e5a7c9e1f3b4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6b8d0f2a4c5"
down_revision: Union[str, Sequence[str], None] = "e5a7c9e1f3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICIES = """
ALTER TABLE exposure_ladders ENABLE ROW LEVEL SECURITY;
ALTER TABLE exposure_ladder_items ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF to_regprocedure('auth.uid()') IS NOT NULL THEN
    EXECUTE 'CREATE POLICY exposure_ladders_own ON exposure_ladders
             USING (account_id = auth.uid()) WITH CHECK (account_id = auth.uid())';
    EXECUTE 'CREATE POLICY exposure_ladder_items_own ON exposure_ladder_items
             USING (EXISTS (SELECT 1 FROM exposure_ladders l WHERE l.id = ladder_id AND l.account_id = auth.uid()))
             WITH CHECK (EXISTS (SELECT 1 FROM exposure_ladders l WHERE l.id = ladder_id AND l.account_id = auth.uid()))';
  END IF;
END $$;
"""


def upgrade() -> None:
    op.create_table(
        "exposure_ladders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("fear_name", sa.Text(), nullable=False),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_exposure_ladders_account_id", "exposure_ladders", ["account_id"])
    op.create_table(
        "exposure_ladder_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ladder_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exposure_ladders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("expected_suds", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_exposure_ladder_items_ladder_id", "exposure_ladder_items", ["ladder_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_POLICIES)


def downgrade() -> None:
    op.drop_index("ix_exposure_ladder_items_ladder_id", table_name="exposure_ladder_items")
    op.drop_table("exposure_ladder_items")
    op.drop_index("ix_exposure_ladders_account_id", table_name="exposure_ladders")
    op.drop_table("exposure_ladders")
