"""billing plan and payment events

Revision ID: 480afda9248d
Revises: abcead0dea8e
Create Date: 2026-11-01 13:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '480afda9248d'
down_revision: Union[str, Sequence[str], None] = 'abcead0dea8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # plan drives the daily limits. Only the Razorpay webhook changes it, from subscription events.
    # razorpay_subscription_id is the subscription currently granting (or last granting) Pro.
    op.execute("""
        ALTER TABLE users
            ADD COLUMN plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro')),
            ADD COLUMN razorpay_subscription_id TEXT UNIQUE,
            ADD COLUMN subscription_status TEXT
    """)
    # Razorpay delivers webhooks at least once. Recording each event ID makes processing
    # idempotent: a redelivered event hits the primary key and is skipped.
    op.execute("""
        CREATE TABLE payment_events (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE payment_events")
    op.execute("""
        ALTER TABLE users
            DROP COLUMN subscription_status,
            DROP COLUMN razorpay_subscription_id,
            DROP COLUMN plan
    """)
