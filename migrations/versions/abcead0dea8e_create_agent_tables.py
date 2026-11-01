"""create agent tables

Revision ID: abcead0dea8e
Revises: c6d4a8b3d581
Create Date: 2026-11-01 11:03:00.061390

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'abcead0dea8e'
down_revision: Union[str, Sequence[str], None] = 'c6d4a8b3d581'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # One row per agent run. status: running -> awaiting_approval <-> running -> done | failed.
    op.execute("""
        CREATE TABLE agent_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL REFERENCES users(id),
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'awaiting_approval', 'done', 'failed')),
            answer TEXT,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ
        )
    """)
    # Serves run history and the daily quota count (runs per user since a time).
    op.execute("CREATE INDEX agent_runs_user_created_idx ON agent_runs (user_id, created_at DESC)")

    # One row per tool call. `call` is the model's raw function-call part (including any thought
    # signature), stored so the conversation can be rebuilt exactly when a run resumes after approval.
    op.execute("""
        CREATE TABLE agent_steps (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step_no INT NOT NULL,
            tool TEXT NOT NULL,
            args JSONB NOT NULL,
            call JSONB NOT NULL,
            output JSONB,
            status TEXT NOT NULL CHECK (status IN ('done', 'awaiting_approval', 'rejected')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (run_id, step_no)
        )
    """)
    # A run waits on at most one write tool at a time; the database enforces it.
    op.execute("""
        CREATE UNIQUE INDEX agent_steps_one_pending_idx ON agent_steps (run_id)
        WHERE status = 'awaiting_approval'
    """)

    # Written by the create_task tool. due is a calendar date, not an instant.
    op.execute("""
        CREATE TABLE tasks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            due DATE,
            source_file_id UUID REFERENCES files(id) ON DELETE SET NULL,
            done BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX tasks_user_created_idx ON tasks (user_id, created_at DESC)")

    # Written by the save_draft tool.
    op.execute("""
        CREATE TABLE drafts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX drafts_user_created_idx ON drafts (user_id, created_at DESC)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE drafts")
    op.execute("DROP TABLE tasks")
    op.execute("DROP TABLE agent_steps")
    op.execute("DROP TABLE agent_runs")
