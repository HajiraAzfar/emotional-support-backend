"""captured value status (answered / skipped / not_asked), unique message positions, message client id

Revision ID: d8e4f2a9c613
Revises: a6c2e9d41f07
Create Date: 2026-10-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd8e4f2a9c613'
down_revision: Union[str, Sequence[str], None] = 'a6c2e9d41f07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('captured_values', sa.Column('status', sa.String(length=10), nullable=False, server_default='answered'))
    op.execute("UPDATE captured_values SET status = 'skipped' WHERE skipped")
    op.add_column('messages', sa.Column('client_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_unique_constraint('uq_messages_client_id', 'messages', ['client_id'])
    # Close any gap or duplicate before the constraint (numbering was count + 1).
    op.execute("""
        UPDATE messages AS m
        SET sequence = r.rn
        FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY entry_id ORDER BY sequence, created_at) AS rn FROM messages) AS r
        WHERE m.id = r.id AND m.sequence <> r.rn
    """)
    op.create_unique_constraint('uq_message_position', 'messages', ['entry_id', 'sequence'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_message_position', 'messages', type_='unique')
    op.drop_constraint('uq_messages_client_id', 'messages', type_='unique')
    op.drop_column('messages', 'client_id')
    op.drop_column('captured_values', 'status')
