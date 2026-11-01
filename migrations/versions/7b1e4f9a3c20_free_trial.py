"""free trial

Revision ID: 7b1e4f9a3c20
Revises: 6e68c7881583
Create Date: 2026-11-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7b1e4f9a3c20'
down_revision: Union[str, Sequence[str], None] = '6e68c7881583'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # One free trial of Pro per account. NULL = never started. Once set it is never cleared, so the
    # same account can't start a second trial; access lasts while trial_ends_at is in the future.
    op.execute("ALTER TABLE users ADD COLUMN trial_ends_at TIMESTAMPTZ")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE users DROP COLUMN trial_ends_at")
