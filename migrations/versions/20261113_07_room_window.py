# 记录实体房是否有窗，供房态图和筛选展示。这是历史迁移，已有数据库按 revision 链顺序执行。
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
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
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
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.drop_column("rooms", "has_window")
