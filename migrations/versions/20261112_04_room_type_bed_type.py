"""Add room type bed type for guest filters

Revision ID: 20261112_04
Revises: 20261112_03
Create Date: 2026-11-12 20:10:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261112_04"
down_revision = "20261112_03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "room_types",
        sa.Column("bed_type", sa.String(length=20), nullable=False, server_default="double"),
    )
    op.create_index("ix_room_types_bed_type", "room_types", ["bed_type"])
    op.create_check_constraint(
        "ck_room_types_bed_type",
        "room_types",
        "bed_type IN ('single', 'double', 'twin', 'king', 'suite')",
    )
    op.execute("UPDATE room_types SET bed_type = 'single' WHERE code = 'demo-single'")
    op.execute("UPDATE room_types SET bed_type = 'double' WHERE code = 'demo-double'")
    op.execute("UPDATE room_types SET bed_type = 'suite' WHERE code = 'demo-family'")
    op.execute("UPDATE room_types SET bed_type = 'king' WHERE code = 'harbour-king'")
    op.execute("UPDATE room_types SET bed_type = 'twin' WHERE code = 'city-twin'")


def downgrade():
    op.drop_constraint("ck_room_types_bed_type", "room_types", type_="check")
    op.drop_index("ix_room_types_bed_type", table_name="room_types")
    op.drop_column("room_types", "bed_type")
