"""create voice_notes

Revision ID: c6d4a8b3d581
Revises: a39bcbe0c67c
Create Date: 2026-10-31 14:46:05.232610

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c6d4a8b3d581'
down_revision: Union[str, Sequence[str], None] = 'a39bcbe0c67c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        create table voice_notes (
            file_id UUID primary key references files(id) on delete cascade,
            summary TEXT not null,
            action_items JSONB not null default '[]'::jsonb,
            topics JSONB not null default '[]'::jsonb,
            created_at TIMESTAMPTZ not null default now()
        )
    """)



def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""
        drop table voice_notes
    """)
