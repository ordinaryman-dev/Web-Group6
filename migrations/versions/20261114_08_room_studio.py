"""Add the studio bed type for the sixth room type

Revision ID: 20261114_08
Revises: 20261113_07
Create Date: 2026-11-14 21:40:00
"""
from alembic import op


revision = "20261114_08"
down_revision = "20261113_07"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_room_types_bed_type", "room_types", type_="check")
    op.create_check_constraint(
        "ck_room_types_bed_type",
        "room_types",
        "bed_type IN ('single', 'double', 'twin', 'king', 'studio', 'suite')",
    )


def downgrade():
    # Keep the rows: a studio type becomes a double type instead of being
    # deleted, so rooms, price rules and bookings keep their links.
    op.execute("UPDATE room_types SET bed_type = 'double' WHERE bed_type = 'studio'")
    op.drop_constraint("ck_room_types_bed_type", "room_types", type_="check")
    op.create_check_constraint(
        "ck_room_types_bed_type",
        "room_types",
        "bed_type IN ('single', 'double', 'twin', 'king', 'suite')",
    )
