"""Add manager console and hotel operations records."""
import sqlalchemy as sa
from alembic import op


revision = "20261112_02"
down_revision = "20261112_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("price_rules", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index("ix_price_rules_scope", "price_rules", ["room_type_id", "kind", "active", "start_date"])
    op.drop_constraint("uq_price_rules_start", "price_rules", type_="unique")
    op.add_column("room_blocks", sa.Column("effective", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("bookings", sa.Column("checked_in_at", sa.DateTime()))
    op.add_column("bookings", sa.Column("checked_out_at", sa.DateTime()))
    op.create_table(
        "booking_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("target", sa.JSON()),
        sa.Column("reason", sa.String(240)),
        sa.Column("submitted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("handled_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("handled_at", sa.DateTime()),
        sa.Column("comment", sa.String(240)),
    )
    op.create_index("ix_booking_requests_booking_id", "booking_requests", ["booking_id"])
    op.create_index("ix_booking_requests_status", "booking_requests", ["status"])
    op.create_table(
        "payment_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("booking_requests.id")),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("result", sa.String(12), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("reference", sa.String(64), nullable=False, unique=True),
        sa.CheckConstraint("kind IN ('payment', 'refund')", name="ck_payment_records_kind"),
        sa.CheckConstraint("amount > 0", name="ck_payment_records_amount"),
        sa.CheckConstraint("result IN ('success', 'failed')", name="ck_payment_records_result"),
    )
    op.create_index("ix_payment_records_booking_id", "payment_records", ["booking_id"])


def downgrade():
    # The older unique key cannot represent revisions of the same price rule.
    # Recreate it first, so a database containing revisions fails before losing data.
    op.create_unique_constraint("uq_price_rules_start", "price_rules", ["room_type_id", "kind", "start_date"])
    op.drop_index("ix_price_rules_scope", table_name="price_rules")
    op.drop_column("price_rules", "active")
    op.drop_table("payment_records")
    op.drop_table("booking_requests")
    op.drop_column("bookings", "checked_out_at")
    op.drop_column("bookings", "checked_in_at")
    op.drop_column("room_blocks", "effective")
