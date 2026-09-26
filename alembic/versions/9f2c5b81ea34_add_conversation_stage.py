"""add conversation stage to entries

Revision ID: 9f2c5b81ea34
Revises: 7b3e1f9c4d20
Create Date: 2026-09-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f2c5b81ea34'
down_revision: Union[str, Sequence[str], None] = '7b3e1f9c4d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('entries', sa.Column('conversation_stage', sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('entries', 'conversation_stage')
