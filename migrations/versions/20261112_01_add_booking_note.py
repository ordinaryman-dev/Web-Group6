"""Add booking note and created_at

Revision ID: 20261112_01
Revises: 5138518c49b2
Create Date: 2026-11-12 10:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261112_01"
down_revision = "5138518c49b2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("note", sa.String(length=100), nullable=True))
    op.add_column(
        "bookings",
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade():
    op.drop_column("bookings", "created_at")
    op.drop_column("bookings", "note")
