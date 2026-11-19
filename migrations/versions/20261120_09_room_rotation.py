"""Remember successful room assignments for load-based rotation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "20261120_09"
down_revision = "20261114_08"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("rooms", sa.Column("last_allocated_at", mysql.DATETIME(fsp=6), nullable=True))
    # Existing orders are the available evidence for an earlier assignment.
    # Unused rooms keep NULL and precede previously assigned rooms on a load tie.
    op.execute("""
        UPDATE rooms AS r
        JOIN (
            SELECT room_id, MAX(created_at) AS assigned_at
            FROM bookings GROUP BY room_id
        ) AS history ON history.room_id = r.id
        SET r.last_allocated_at = history.assigned_at
    """)


def downgrade():
    op.drop_column("rooms", "last_allocated_at")
