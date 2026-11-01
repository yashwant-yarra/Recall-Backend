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
    # Three tiers: free, pro (new, in the middle) and ultra (the limits 'pro' used to mean). Everyone who
    # is 'pro' today is paying for those old limits through the existing Razorpay plan, so they become
    # 'ultra' and lose nothing. The constraint goes first so the new value is allowed.
    op.execute("ALTER TABLE users DROP CONSTRAINT users_plan_check")
    op.execute("UPDATE users SET plan = 'ultra' WHERE plan = 'pro'")
    op.execute("ALTER TABLE users ADD CONSTRAINT users_plan_check CHECK (plan IN ('free', 'pro', 'ultra'))")


def downgrade() -> None:
    """Downgrade schema."""
    # Ultra becomes pro again (the old, single paid tier); the new middle pro would lose its meaning,
    # so it becomes free. Order matters: rewrite the values, then tighten the constraint.
    op.execute("UPDATE users SET plan = 'free' WHERE plan = 'pro'")
    op.execute("UPDATE users SET plan = 'pro' WHERE plan = 'ultra'")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_plan_check")
    op.execute("ALTER TABLE users ADD CONSTRAINT users_plan_check CHECK (plan IN ('free', 'pro'))")
