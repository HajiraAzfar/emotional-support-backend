"""helplines card as its own field on messages

Revision ID: f3a9c1d27b85
Revises: b71d90c4e532
Create Date: 2026-10-10 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d27b85'
down_revision: Union[str, Sequence[str], None] = 'b71d90c4e532'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('messages', sa.Column('card', sa.String(length=10), nullable=True))
    # Cards were briefly stored as their own "support" messages with the label as
    # their text, which an app without the card code showed as a bubble ("soft").
    # Move each onto the message it sat under, drop those rows, and close the gaps
    # in the numbering (new messages are numbered count + 1).
    op.execute("""
        UPDATE messages AS m
        SET card = CASE s.content WHEN 'urgent' THEN 'prominent' ELSE s.content END
        FROM messages AS s
        WHERE s.kind = 'support' AND s.entry_id = m.entry_id AND m.sequence = s.sequence - 1
    """)
    op.execute("DELETE FROM messages WHERE kind = 'support'")
    op.execute("""
        UPDATE messages AS m
        SET sequence = r.rn
        FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY entry_id ORDER BY sequence) AS rn FROM messages) AS r
        WHERE m.id = r.id AND m.sequence <> r.rn
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('messages', 'card')
