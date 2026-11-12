"""Front desk: expected arrival time on a booking

Revision ID: 20261112_03
Revises: 20261112_02
Create Date: 2026-11-12 18:20:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261112_03"
down_revision = "20261112_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("expected_arrival", sa.Time(), nullable=True))


def downgrade():
    op.drop_column("bookings", "expected_arrival")
