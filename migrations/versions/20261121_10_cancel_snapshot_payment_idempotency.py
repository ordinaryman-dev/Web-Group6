"""Snapshot cancellation deadlines and make payment writes idempotent.

Revision ID: 20261121_10
Revises: 20261120_09
Create Date: 2026-11-21 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261121_10"
down_revision = "20261120_09"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("cancel_by", sa.DateTime(), nullable=True))
    # Existing orders keep the deadline that their current arrival date implies.
    # New orders store the deadline at booking time; a later date change can only
    # move it earlier.
    op.execute("""
        UPDATE bookings
        SET cancel_by = DATE_ADD(DATE_SUB(check_in, INTERVAL 1 DAY), INTERVAL 18 HOUR)
        WHERE cancel_by IS NULL
    """)

    op.add_column(
        "payment_records", sa.Column("idempotency_key", sa.String(length=64), nullable=True)
    )
    op.create_unique_constraint(
        "uq_payment_actor_key",
        "payment_records",
        ["actor_user_id", "idempotency_key"],
    )


def downgrade():
    op.drop_constraint("uq_payment_actor_key", "payment_records", type_="unique")
    op.drop_column("payment_records", "idempotency_key")
    op.drop_column("bookings", "cancel_by")
