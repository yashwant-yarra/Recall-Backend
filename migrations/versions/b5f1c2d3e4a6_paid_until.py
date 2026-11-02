"""prepaid passes: users.paid_until

Revision ID: b5f1c2d3e4a6
Revises: 9d3e6b7f2a42
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b5f1c2d3e4a6'
down_revision: Union[str, Sequence[str], None] = '9d3e6b7f2a42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Paid tiers are now prepaid passes (Cashfree payment links): users.plan says which tier was bought and
    # paid_until says when it runs out. NULL = never paid. db.TIER counts plan only while paid_until > now().
    op.execute("ALTER TABLE users ADD COLUMN paid_until TIMESTAMPTZ")
    # Anyone on a paid plan from the Razorpay subscriptions keeps it for one more pass, so nobody loses
    # access the moment this runs. The Razorpay columns stay (unused) until a later cleanup.
    op.execute("UPDATE users SET paid_until = now() + interval '30 days' WHERE plan <> 'free'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE users DROP COLUMN paid_until")
