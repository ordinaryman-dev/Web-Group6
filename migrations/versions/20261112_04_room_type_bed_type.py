# 为房型增加床型，支持客户和员工按床型筛选。这是历史迁移，已有数据库按 revision 链顺序执行。
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
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
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
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.drop_constraint("ck_room_types_bed_type", "room_types", type_="check")
    op.drop_index("ix_room_types_bed_type", table_name="room_types")
    op.drop_column("room_types", "bed_type")
