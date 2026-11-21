# 增加最近成功分房时间，并用已有订单初始化历史值，支持负载相同时轮换。这是历史迁移，已有数据库按 revision 链顺序执行。
"""Remember successful room assignments for load-based rotation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "20261120_09"
down_revision = "20261114_08"
branch_labels = None
depends_on = None


def upgrade():
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
    op.add_column("rooms", sa.Column("last_allocated_at", mysql.DATETIME(fsp=6), nullable=True))
    # Existing orders are the available evidence for an earlier assignment.
    # Unused rooms keep NULL and precede previously assigned rooms on a load tie.
    op.execute("""
        UPDATE rooms AS r
        JOIN (
            SELECT room_id, MAX(created_at) AS assigned_at
            FROM bookings GROUP BY room_id
        ) AS history ON history.room_id = r.id
        SET r.last_allocated_at = history.assigned_at
    """)


def downgrade():
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.drop_column("rooms", "last_allocated_at")
