# 扩展床型选项，支持 Studio 房型。这是历史迁移，已有数据库按 revision 链顺序执行。
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
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
    op.drop_constraint("ck_room_types_bed_type", "room_types", type_="check")
    op.create_check_constraint(
        "ck_room_types_bed_type",
        "room_types",
        "bed_type IN ('single', 'double', 'twin', 'king', 'studio', 'suite')",
    )


def downgrade():
    # Keep the rows: a studio type becomes a double type instead of being
    # deleted, so rooms, price rules and bookings keep their links.
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.execute("UPDATE room_types SET bed_type = 'double' WHERE bed_type = 'studio'")
    op.drop_constraint("ck_room_types_bed_type", "room_types", type_="check")
    op.create_check_constraint(
        "ck_room_types_bed_type",
        "room_types",
        "bed_type IN ('single', 'double', 'twin', 'king', 'suite')",
    )
