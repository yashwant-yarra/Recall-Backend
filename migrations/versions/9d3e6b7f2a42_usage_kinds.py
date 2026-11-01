"""usage kinds

Revision ID: 9d3e6b7f2a42
Revises: 8c2d5a6e1f31
Create Date: 2026-11-02 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9d3e6b7f2a42'
down_revision: Union[str, Sequence[str], None] = '8c2d5a6e1f31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "('transcribe', 'tts', 'live')"
NEW = "('transcribe', 'tts', 'live', 'upload', 'question', 'quiz', 'agent', 'web')"


def upgrade() -> None:
    """Upgrade schema."""
    # Daily limits for uploads, questions, quizzes, agent runs and web-search answers are now counted here
    # (one row per use) instead of from the files, messages, runs and quizzes themselves. 'Clear all data'
    # deletes those, so counting them let anyone reset a limit by clearing; this ledger is never cleared.
    op.execute("ALTER TABLE usage_events DROP CONSTRAINT usage_events_kind_check")
    op.execute(f"ALTER TABLE usage_events ADD CONSTRAINT usage_events_kind_check CHECK (kind IN {NEW})")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM usage_events WHERE kind NOT IN ('transcribe', 'tts', 'live')")
    op.execute("ALTER TABLE usage_events DROP CONSTRAINT usage_events_kind_check")
    op.execute(f"ALTER TABLE usage_events ADD CONSTRAINT usage_events_kind_check CHECK (kind IN {OLD})")
