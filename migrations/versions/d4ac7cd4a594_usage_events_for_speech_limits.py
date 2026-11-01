"""usage events for speech limits

Revision ID: d4ac7cd4a594
Revises: deef85efdac1
Create Date: 2026-11-01 16:10:58.755209

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4ac7cd4a594'
down_revision: Union[str, Sequence[str], None] = 'deef85efdac1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # One row per metered call that leaves no other trace (voice input, read-aloud), so daily
    # limits can count them. Uploads, questions, quizzes and agent runs are counted from their
    # own tables.
    op.execute("""
        CREATE TABLE usage_events (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL CHECK (kind IN ('transcribe', 'tts')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX usage_events_user_kind_created_idx ON usage_events (user_id, kind, created_at)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE usage_events")
