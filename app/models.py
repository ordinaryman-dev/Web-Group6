# 数据库表结构：模型描述字段、关系和约束；修改结构后要新增迁移，不能只改 Python 文件。
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy.dialects.mysql import DATETIME

from .extensions import db


GUEST = "guest"
STAFF = "staff"
MANAGER = "manager"


def utc_now():
    # The schema uses second-precision DATETIME. Avoid rounding writes into the future.
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class User(UserMixin, db.Model):
    # 登录账号和角色；停用账号保留历史关联，不直接删除操作记录。
    __tablename__ = "users"
    __table_args__ = (
        db.CheckConstraint("role IN ('guest', 'staff', 'manager')", name="ck_users_role"),
    )

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def is_active(self):
        return self.active


class Customer(db.Model):
    # 入住客户档案；线上客户关联账号，线下客户也可只有档案而没有登录账号。
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.ForeignKey("users.id"), unique=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(40), index=True)
    user = db.relationship(User)


class CustomerPhoneLock(db.Model):
    # 同手机号新建档案的互斥记录；摘要只是并发协调键，不等于身份证明。
    """Coordinate walk-in registration without making a phone a unique identity."""
    __tablename__ = "customer_phone_locks"

    lock_key = db.Column(db.String(64), primary_key=True)


class RoomType(db.Model):
    # 公开销售的房型，包括容量、床型和基础价格；一类房型可以对应多间实体房。
    __tablename__ = "room_types"
    __table_args__ = (
        db.CheckConstraint("capacity > 0", name="ck_room_types_capacity"),
        db.CheckConstraint("base_price >= 0", name="ck_room_types_price"),
        db.CheckConstraint(
            "bed_type IN ('single', 'double', 'twin', 'king', 'studio', 'suite')",
            name="ck_room_types_bed_type",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    base_price = db.Column(db.Numeric(10, 2), nullable=False)
    listed = db.Column(db.Boolean, nullable=False, default=True)
    bed_type = db.Column(db.String(20), nullable=False, default="double", index=True)


class Room(db.Model):
    # 实体房间及运行、清洁状态；last_allocated_at 用于自动分房轮换。
    __tablename__ = "rooms"
    __table_args__ = (
        db.CheckConstraint(
            "operational_until IS NULL OR operational_until > operational_from",
            name="ck_rooms_operational_range",
        ),
        db.CheckConstraint("housekeeping IN ('clean', 'dirty')", name="ck_rooms_housekeeping"),
    )

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(20), unique=True, nullable=False)
    room_type_id = db.Column(db.ForeignKey("room_types.id"), index=True, nullable=False)
    operational_from = db.Column(db.Date, nullable=False)
    operational_until = db.Column(db.Date)
    housekeeping = db.Column(
        db.String(16), nullable=False, default="clean", server_default="clean"
    )
    has_window = db.Column(
        db.Boolean, nullable=False, default=True, server_default=db.true()
    )
    last_allocated_at = db.Column(db.DateTime().with_variant(DATETIME(fsp=6), "mysql"))


class PriceRule(db.Model):
    # 按日期区间生效的价格规则；修改后只影响重新计算的报价，不改旧订单快照。
    __tablename__ = "price_rules"
    __table_args__ = (
        db.CheckConstraint("end_date > start_date", name="ck_price_rules_range"),
        db.CheckConstraint("kind IN ('season', 'date')", name="ck_price_rules_kind"),
        db.CheckConstraint("amount >= 0", name="ck_price_rules_amount"),
        db.Index("ix_price_rules_scope", "room_type_id", "kind", "active", "start_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    room_type_id = db.Column(db.ForeignKey("room_types.id"), nullable=False)
    kind = db.Column(db.String(12), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())


class RoomBlock(db.Model):
    # 维修封锁区间；失效时保留记录，已有订单冲突需要另行处理。
    __tablename__ = "room_blocks"
    __table_args__ = (db.CheckConstraint("end_date > start_date", name="ck_room_blocks_range"),)

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.ForeignKey("rooms.id"), index=True, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(240), nullable=False)
    created_by_id = db.Column(db.ForeignKey("users.id"))
    effective = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    room = db.relationship("Room")
    creator = db.relationship("User", foreign_keys=[created_by_id])


class Booking(db.Model):
    # 订单主记录；操作人加幂等键防止重复创建，取消期限和确认价格是订单约定。
    __tablename__ = "bookings"
    __table_args__ = (
        db.UniqueConstraint("actor_user_id", "idempotency_key", name="uq_booking_actor_key"),
        db.CheckConstraint("check_out > check_in", name="ck_bookings_stay_range"),
    )

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.ForeignKey("customers.id"), nullable=False)
    room_id = db.Column(db.ForeignKey("rooms.id"), nullable=False)
    room_type_id = db.Column(db.ForeignKey("room_types.id"), nullable=False)
    actor_user_id = db.Column(db.ForeignKey("users.id"), nullable=False)
    idempotency_key = db.Column(db.String(64), nullable=False)
    request_digest = db.Column(db.String(64), nullable=False)
    check_in = db.Column(db.Date, nullable=False)
    check_out = db.Column(db.Date, nullable=False)
    # Snapshot of the free-cancellation deadline agreed when the booking was made.
    # A later date change may only bring it forward, never extend it.
    cancel_by = db.Column(db.DateTime)
    guests = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="confirmed")
    total = db.Column(db.Numeric(12, 2), nullable=False)
    note = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    expected_arrival = db.Column(db.Time)
    checked_in_at = db.Column(db.DateTime)
    checked_out_at = db.Column(db.DateTime)
    no_show_at = db.Column(db.DateTime)
    no_show_reason = db.Column(db.String(240))
    last_contact_at = db.Column(db.DateTime)
    last_contact_result = db.Column(db.String(40))

    customer = db.relationship(Customer, lazy="joined")
    room = db.relationship(Room, lazy="joined")
    room_type = db.relationship(RoomType, lazy="joined")
    price_lines = db.relationship(
        "BookingPriceLine", order_by="BookingPriceLine.stay_date", lazy="selectin"
    )

    @property
    def reference(self):
        return f"BK-{self.id:06d}"

    @property
    def nights(self):
        return (self.check_out - self.check_in).days


class RoomNight(db.Model):
    # 一间房一晚的占用；(room_id, stay_date) 唯一约束是防止重复占房的最后保障。
    __tablename__ = "room_nights"
    __table_args__ = (db.UniqueConstraint("room_id", "stay_date", name="uq_room_night"),)

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.ForeignKey("rooms.id"), nullable=False)
    stay_date = db.Column(db.Date, nullable=False)
    booking_id = db.Column(db.ForeignKey("bookings.id"), index=True, nullable=False)


class BookingPriceLine(db.Model):
    # 订单逐晚价格快照；不能用当前房价覆盖已经确认的历史金额。
    __tablename__ = "booking_price_lines"
    __table_args__ = (
        db.UniqueConstraint("booking_id", "stay_date", name="uq_booking_price_night"),
    )

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.ForeignKey("bookings.id"), nullable=False)
    stay_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    price_rule_id = db.Column(db.ForeignKey("price_rules.id"))


class AuditLog(db.Model):
    # 只读操作历史：记录操作人、动作、对象、时间和原因。
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.ForeignKey("users.id"), index=True, nullable=False)
    action = db.Column(db.String(48), nullable=False)
    object_type = db.Column(db.String(32), nullable=False)
    object_id = db.Column(db.Integer, nullable=False)
    result = db.Column(db.String(20), nullable=False, default="success")
    occurred_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    reason = db.Column(db.String(240))
    details = db.Column(db.JSON)
    actor = db.relationship(User)


class BlacklistEntry(db.Model):
    # 黑名单生效及解除历史；限制新增业务，但不删除客户原有订单。
    __tablename__ = "blacklist_entries"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.ForeignKey("customers.id"), index=True, nullable=False)
    actor_user_id = db.Column(db.ForeignKey("users.id"), nullable=False)
    reason = db.Column(db.String(240), nullable=False)
    effective_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    removed_at = db.Column(db.DateTime)
    customer = db.relationship(Customer)


class BookingRequest(db.Model):
    # 客户申请队列；target 保存期望修改值，pending 阶段不改原订单。
    __tablename__ = "booking_requests"

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.ForeignKey("bookings.id"), nullable=False, index=True)
    kind = db.Column(db.String(24), nullable=False)
    target = db.Column(db.JSON)
    reason = db.Column(db.String(240))
    submitted_by = db.Column(db.ForeignKey("users.id"), nullable=False)
    submitted_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    handled_by = db.Column(db.ForeignKey("users.id"))
    handled_at = db.Column(db.DateTime)
    comment = db.Column(db.String(240))

    booking = db.relationship("Booking")
    submitter = db.relationship("User", foreign_keys=[submitted_by])


class PaymentRecord(db.Model):
    # 模拟收款与退款流水；实际可退金额由成功收款减去成功退款得到。
    __tablename__ = "payment_records"
    __table_args__ = (
        db.CheckConstraint("kind IN ('payment', 'refund')", name="ck_payment_records_kind"),
        db.CheckConstraint("amount > 0", name="ck_payment_records_amount"),
        db.CheckConstraint("result IN ('success', 'failed')", name="ck_payment_records_result"),
        db.UniqueConstraint("actor_user_id", "idempotency_key", name="uq_payment_actor_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.ForeignKey("bookings.id"), nullable=False, index=True)
    request_id = db.Column(db.ForeignKey("booking_requests.id"))
    kind = db.Column(db.String(12), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    actor_user_id = db.Column(db.ForeignKey("users.id"), nullable=False)
    result = db.Column(db.String(12), nullable=False, default="success")
    occurred_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    reference = db.Column(db.String(64), nullable=False, unique=True)
    idempotency_key = db.Column(db.String(64))
