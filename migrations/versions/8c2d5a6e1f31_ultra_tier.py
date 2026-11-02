"""ultra tier

Revision ID: 8c2d5a6e1f31
Revises: 7b1e4f9a3c20
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '8c2d5a6e1f31'
down_revision: Union[str, Sequence[str], None] = '7b1e4f9a3c20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Three tiers: free, pro and ultra. Existing 'pro' users stay 'pro': they pay for the original Razorpay
    # plan, which is now the Pro plan, so their plan value already says what they are paying for. Only the
    # constraint changes, to allow the new 'ultra' value (sold through a new Razorpay plan).
    op.execute("ALTER TABLE users DROP CONSTRAINT users_plan_check")
    op.execute("ALTER TABLE users ADD CONSTRAINT users_plan_check CHECK (plan IN ('free', 'pro', 'ultra'))")


def downgrade() -> None:
    """Downgrade schema."""
    # There is no 'ultra' before this revision; those users fall back to 'pro', the highest tier that existed.
    op.execute("UPDATE users SET plan = 'pro' WHERE plan = 'ultra'")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_plan_check")
    op.execute("ALTER TABLE users ADD CONSTRAINT users_plan_check CHECK (plan IN ('free', 'pro'))")
