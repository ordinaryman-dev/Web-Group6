# 为订单增加最多 100 字的客户备注字段。这是历史迁移，已有数据库按 revision 链顺序执行。
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
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
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
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.drop_column("bookings", "created_at")
    op.drop_column("bookings", "note")
