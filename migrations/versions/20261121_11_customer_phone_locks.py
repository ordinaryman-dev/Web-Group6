"""Coordinate new walk-in registrations for the same contact number."""
from alembic import op
import sqlalchemy as sa


revision = "20261121_11"
down_revision = "20261121_10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "customer_phone_locks",
        sa.Column("lock_key", sa.String(64), primary_key=True, nullable=False),
    )
    # Reconcile old terminal orders as well as protecting future transitions.
    # No human decision is invented: the comment identifies this as upgrade work.
    op.execute("""
        UPDATE booking_requests AS request
        JOIN bookings AS booking ON booking.id = request.booking_id
        SET request.status = 'closed',
            request.handled_at = COALESCE(request.handled_at, UTC_TIMESTAMP()),
            request.comment = 'Closed during upgrade because the booking had already ended.'
        WHERE request.status = 'pending'
          AND booking.status IN ('cancelled', 'checked_out', 'no_show')
    """)


def downgrade():
    op.drop_table("customer_phone_locks")
