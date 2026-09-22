"""check-in capture flow, conversation state, crisis tiers, user terms

Revision ID: 7b3e1f9c4d20
Revises: 2a7e2d3ce106
Create Date: 2026-09-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b3e1f9c4d20'
down_revision: Union[str, Sequence[str], None] = '2a7e2d3ce106'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('messages', sa.Column('kind', sa.String(length=20), nullable=True))
    op.add_column('messages', sa.Column('value_id', sa.String(length=50), nullable=True))

    op.alter_column('captured_values', 'value', existing_type=sa.String(length=255), type_=sa.Text(), nullable=True)
    op.add_column('captured_values', sa.Column('skipped', sa.Boolean(), server_default=sa.false(), nullable=False))

    op.add_column('entries', sa.Column('conversation_status', sa.String(length=20), nullable=True))
    op.add_column('entries', sa.Column('closure_reason', sa.String(length=40), nullable=True))
    op.add_column('entries', sa.Column('crisis_tier', sa.String(length=12), nullable=True))

    op.create_table(
        'crisis_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('account_id', sa.UUID(), nullable=False),
        sa.Column('entry_id', sa.UUID(), nullable=True),
        sa.Column('field', sa.String(length=50), nullable=False),
        sa.Column('tier', sa.String(length=12), nullable=False),
        sa.Column('variant', sa.String(length=12), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='crisis_events_account_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['entry_id'], ['entries.id'], name='crisis_events_entry_id_fkey', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_crisis_events_account_id', 'crisis_events', ['account_id'])
    op.create_index('ix_crisis_events_entry_id', 'crisis_events', ['entry_id'])

    op.create_table(
        'user_terms',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('account_id', sa.UUID(), nullable=False),
        sa.Column('library', sa.String(length=30), nullable=False),
        sa.Column('name', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='user_terms_account_id_fkey', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'library', 'name', name='uq_user_term'),
    )
    op.create_index('ix_user_terms_account_id', 'user_terms', ['account_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_user_terms_account_id', table_name='user_terms')
    op.drop_table('user_terms')
    op.drop_index('ix_crisis_events_entry_id', table_name='crisis_events')
    op.drop_index('ix_crisis_events_account_id', table_name='crisis_events')
    op.drop_table('crisis_events')

    op.drop_column('entries', 'crisis_tier')
    op.drop_column('entries', 'closure_reason')
    op.drop_column('entries', 'conversation_status')

    op.drop_column('captured_values', 'skipped')
    op.execute("UPDATE captured_values SET value = '' WHERE value IS NULL")
    op.alter_column('captured_values', 'value', existing_type=sa.Text(), type_=sa.String(length=255), nullable=False)

    op.drop_column('messages', 'value_id')
    op.drop_column('messages', 'kind')
