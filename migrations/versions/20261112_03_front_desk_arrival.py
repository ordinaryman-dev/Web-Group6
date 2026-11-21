# 为订单增加预计到店时间，供员工判断联系及未到店处理时机。这是历史迁移，已有数据库按 revision 链顺序执行。
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
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
    op.add_column("bookings", sa.Column("expected_arrival", sa.Time(), nullable=True))


def downgrade():
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.drop_column("bookings", "expected_arrival")
