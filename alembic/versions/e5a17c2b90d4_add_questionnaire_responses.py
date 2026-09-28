"""add questionnaire responses

FR-INS-017/018: each offer of the wellbeing questionnaire, completed or
declined, so the next one is due fourteen days after the last of either.

Revision ID: e5a17c2b90d4
Revises: c4d81a6f37b2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a17c2b90d4"
down_revision: Union[str, Sequence[str], None] = "c4d81a6f37b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "questionnaire_responses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=12), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("max_score", sa.Integer(), nullable=True),
        sa.Column("answers", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_questionnaire_responses_account_id"),
        "questionnaire_responses",
        ["account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_questionnaire_responses_account_id"),
        table_name="questionnaire_responses",
    )
    op.drop_table("questionnaire_responses")
