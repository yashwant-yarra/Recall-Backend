"""live voice minutes

Revision ID: 6e68c7881583
Revises: d4ac7cd4a594
Create Date: 2026-11-01 16:52:44.814799

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e68c7881583'
down_revision: Union[str, Sequence[str], None] = 'd4ac7cd4a594'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Live voice sessions are metered by length, not count: one row per session, its `seconds`
    # filled in when it ends. The daily limit sums them.
    op.execute("""
        ALTER TABLE usage_events
            DROP CONSTRAINT usage_events_kind_check,
            ADD CONSTRAINT usage_events_kind_check CHECK (kind IN ('transcribe', 'tts', 'live')),
            ADD COLUMN seconds INTEGER
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM usage_events WHERE kind = 'live'")
    op.execute("""
        ALTER TABLE usage_events
            DROP COLUMN seconds,
            DROP CONSTRAINT usage_events_kind_check,
            ADD CONSTRAINT usage_events_kind_check CHECK (kind IN ('transcribe', 'tts'))
    """)
