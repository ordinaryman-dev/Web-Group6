"""Front desk: block operator, no-show handling and contact log

Revision ID: 20261112_05
Revises: 20261112_04
Create Date: 2026-11-12 23:05:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261112_05"
down_revision = "20261112_04"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("room_blocks", sa.Column("created_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_room_blocks_created_by", "room_blocks", "users", ["created_by_id"], ["id"]
    )

    op.add_column("bookings", sa.Column("no_show_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("no_show_reason", sa.String(length=240), nullable=True))
    op.add_column("bookings", sa.Column("last_contact_at", sa.DateTime(), nullable=True))
    op.add_column(
        "bookings", sa.Column("last_contact_result", sa.String(length=40), nullable=True)
    )


def downgrade():
    op.drop_column("bookings", "last_contact_result")
    op.drop_column("bookings", "last_contact_at")
    op.drop_column("bookings", "no_show_reason")
    op.drop_column("bookings", "no_show_at")
    op.drop_constraint("fk_room_blocks_created_by", "room_blocks", type_="foreignkey")
    op.drop_column("room_blocks", "created_by_id")
