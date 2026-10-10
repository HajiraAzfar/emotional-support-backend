"""client id on entries, so a retried check-in is saved once

Revision ID: a6c2e9d41f07
Revises: f3a9c1d27b85
Create Date: 2026-10-10 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a6c2e9d41f07'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1d27b85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('entries', sa.Column('client_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_unique_constraint('uq_entries_client_id', 'entries', ['client_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_entries_client_id', 'entries', type_='unique')
    op.drop_column('entries', 'client_id')
