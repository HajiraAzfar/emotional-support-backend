"""add exposure cycle link and notice acknowledgement

Revision ID: c4d81a6f37b2
Revises: 9f2c5b81ea34
Create Date: 2026-09-26 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d81a6f37b2'
down_revision: Union[str, Sequence[str], None] = '9f2c5b81ea34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('entries', sa.Column('parent_entry_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'entries_parent_entry_id_fkey', 'entries', 'entries',
        ['parent_entry_id'], ['id'], ondelete='SET NULL',
    )
    op.add_column(
        'entries',
        sa.Column('notice_acknowledged', sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('entries', 'notice_acknowledged')
    op.drop_constraint('entries_parent_entry_id_fkey', 'entries', type_='foreignkey')
    op.drop_column('entries', 'parent_entry_id')