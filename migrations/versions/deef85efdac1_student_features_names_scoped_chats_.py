"""student features: names, scoped chats, quizzes

Revision ID: deef85efdac1
Revises: 480afda9248d
Create Date: 2026-11-01 15:33:25.252063

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'deef85efdac1'
down_revision: Union[str, Sequence[str], None] = '480afda9248d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # display_name: the client's file name, for display only. Storage paths never use it.
    op.execute("ALTER TABLE files ADD COLUMN display_name TEXT")
    # A chat can be about one document. If that file is deleted, the chat stays, unscoped.
    op.execute("ALTER TABLE chats ADD COLUMN file_id UUID REFERENCES files(id) ON DELETE SET NULL")
    # Practice quizzes. questions = generated questions with answers and page refs; answers and
    # results are filled in once, when the student submits.
    op.execute("""
        CREATE TABLE quizzes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL REFERENCES users(id),
            file_id UUID REFERENCES files(id) ON DELETE SET NULL,
            topic TEXT,
            questions JSONB NOT NULL,
            answers JSONB,
            results JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            graded_at TIMESTAMPTZ
        )
    """)
    # Serves quiz history and the daily quiz limit.
    op.execute("CREATE INDEX quizzes_user_created_idx ON quizzes (user_id, created_at DESC)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE quizzes")
    op.execute("ALTER TABLE chats DROP COLUMN file_id")
    op.execute("ALTER TABLE files DROP COLUMN display_name")
