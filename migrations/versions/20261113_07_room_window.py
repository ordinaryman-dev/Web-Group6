"""Room window flag for the front-desk ventilation filter

Revision ID: 20261113_07
Revises: 20261113_06
Create Date: 2026-11-13 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261113_07"
down_revision = "20261113_06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "rooms",
        sa.Column(
            "has_window",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade():
    op.drop_column("rooms", "has_window")
