# 记录实体房的清洁状态，退房后先标脏，再由员工确认清洁。这是历史迁移，已有数据库按 revision 链顺序执行。
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
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
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
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.drop_constraint("ck_rooms_housekeeping", "rooms", type_="check")
    op.drop_column("rooms", "housekeeping")
