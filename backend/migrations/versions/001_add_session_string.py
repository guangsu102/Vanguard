"""Add session_string field to telegram_account

Revision ID: 001_add_session_string
Revises:
Create Date: 2026-05-25

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001_add_session_string'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add session_string column to telegram_account table."""
    op.add_column(
        'telegram_account',
        sa.Column('session_string', sa.Text(), nullable=True, comment='Telethon session string (用于快速恢复登录)')
    )


def downgrade() -> None:
    """Remove session_string column from telegram_account table."""
    op.drop_column('telegram_account', 'session_string')
