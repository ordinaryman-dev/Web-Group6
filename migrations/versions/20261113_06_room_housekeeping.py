"""Room housekeeping state (clean / dirty)

Revision ID: 20261113_06
Revises: 20261112_05
Create Date: 2026-11-13 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261113_06"
down_revision = "20261112_05"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "rooms",
        sa.Column(
            "housekeeping",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'clean'"),
        ),
    )
    op.create_check_constraint(
        "ck_rooms_housekeeping", "rooms", "housekeeping IN ('clean', 'dirty')"
    )


def downgrade():
    op.drop_constraint("ck_rooms_housekeeping", "rooms", type_="check")
    op.drop_column("rooms", "housekeeping")
