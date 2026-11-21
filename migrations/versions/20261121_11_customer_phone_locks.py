# 增加同手机号建档互斥表，并关闭已结束订单遗留的待处理申请。这是历史迁移，已有数据库按 revision 链顺序执行。
"""Coordinate new walk-in registrations for the same contact number."""
from alembic import op
import sqlalchemy as sa


revision = "20261121_11"
down_revision = "20261121_10"
branch_labels = None
depends_on = None


def upgrade():
    # 升级只执行这一版需要的变更；日常更新通过 flask db upgrade 运行。
    op.create_table(
        "customer_phone_locks",
        sa.Column("lock_key", sa.String(64), primary_key=True, nullable=False),
    )
    # Reconcile old terminal orders as well as protecting future transitions.
    # No human decision is invented: the comment identifies this as upgrade work.
    op.execute("""
        UPDATE booking_requests AS request
        JOIN bookings AS booking ON booking.id = request.booking_id
        SET request.status = 'closed',
            request.handled_at = COALESCE(request.handled_at, UTC_TIMESTAMP()),
            request.comment = 'Closed during upgrade because the booking had already ended.'
        WHERE request.status = 'pending'
          AND booking.status IN ('cancelled', 'checked_out', 'no_show')
    """)


def downgrade():
    # 按此版本定义恢复上一版结构；数据修复不一定能够自动逆转。
    op.drop_table("customer_phone_locks")
