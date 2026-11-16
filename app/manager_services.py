from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, lazyload
from werkzeug.security import check_password_hash, generate_password_hash

from .audit import record_audit
from .booking import customer_is_blocked
from .extensions import db
from .models import (
    BlacklistEntry, Booking, Customer, MANAGER, PriceRule,
    Room, RoomBlock, RoomNight, RoomType, STAFF, User, utc_now,
)


class ManagerRejected(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


@contextmanager
def manager_transaction(actor_id, *, change_own_password=False):
    engine = db.engine.execution_options(isolation_level="READ COMMITTED")
    with Session(engine) as session, session.begin():
        actor = session.get(User, actor_id, with_for_update={"read": not change_own_password})
        if actor is None or not actor.active or actor.role != MANAGER:
            raise ManagerRejected("An active manager account is required.", 403)
        yield session


def locked_row(session, model, record_id):
    row = session.get(model, record_id, options=(lazyload("*"),), with_for_update=True)
    if row is None:
        raise ManagerRejected("This record could not be found.", 404)
    return row


def audit_values(record, fields):
    values = {}
    for field in fields:
        value = getattr(record, field)
        if isinstance(value, Decimal):
            value = format(value, ".2f")
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        values[field] = value
    return values


def save_room_type(session, actor_id, record_id, form):
    fields = ("code", "name", "description", "capacity", "bed_type", "base_price", "listed")
    room_type = locked_row(session, RoomType, record_id) if record_id else RoomType()
    before = audit_values(room_type, fields) if record_id else None
    for field in fields:
        setattr(room_type, field, getattr(form, field).data)
    session.add(room_type)
    session.flush()
    record_audit(
        session, actor_id, "edit_room_type" if record_id else "create_room_type", "room_type", room_type.id,
        details={"before": before, "after": audit_values(room_type, fields)},
    )


def save_room(session, actor_id, record_id, form, today):
    fields = ("number", "room_type_id", "operational_from", "operational_until")
    new_type = form.room_type_id.data
    previous_type = session.scalar(select(Room.room_type_id).where(Room.id == record_id)) if record_id else None
    if record_id and previous_type is None:
        raise ManagerRejected("This record could not be found.", 404)
    for type_id in sorted({new_type, previous_type or new_type}):
        if session.get(RoomType, type_id, with_for_update={"read": True}) is None:
            raise ManagerRejected("Choose an existing room type.")
    room = locked_row(session, Room, record_id) if record_id else Room()
    if record_id and room.room_type_id != previous_type:
        raise ManagerRejected("The room type changed. Reload the room and try again.")
    before = audit_values(room, fields) if record_id else None
    start, end = form.operational_from.data, form.operational_until.data
    if (not record_id or start != room.operational_from) and start < today:
        raise ManagerRejected("A new operating start date cannot be in the past.")
    if end and end <= start:
        raise ManagerRejected("The first unavailable date must follow the operating start date.")
    if record_id:
        has_bookings = session.scalar(select(Booking.id).where(Booking.room_id == room.id).limit(1))
        has_blocks = session.scalar(select(RoomBlock.id).where(RoomBlock.room_id == room.id).limit(1))
        has_history = has_bookings is not None or has_blocks is not None
        if (has_history or room.operational_from < today) and (
            room.room_type_id != new_type or room.operational_from != start
        ):
            raise ManagerRejected("This room already has operating history. Keep its room type and start date.")
        if has_history and room.number != form.number.data:
            raise ManagerRejected("Keep the room number associated with existing bookings or maintenance.")
        if end != room.operational_until and (
            (room.operational_until is not None and room.operational_until <= today) or (end and end < today)
        ):
            raise ManagerRejected("A past operating period cannot be rewritten.")
        if end and end != room.operational_until and session.scalar(
            select(RoomNight.id).where(RoomNight.room_id == room.id, RoomNight.stay_date >= end).limit(1)
        ) is not None:
            raise ManagerRejected("Existing bookings extend beyond that operating period.")
    for field in fields:
        setattr(room, field, getattr(form, field).data)
    session.add(room)
    session.flush()
    record_audit(
        session, actor_id, "edit_room" if record_id else "create_room", "room", room.id,
        details={"before": before, "after": audit_values(room, fields)},
    )


def save_price_rule(session, actor_id, rule_id, room_type_id, form):
    locked_row(session, RoomType, room_type_id)
    old_rule = session.get(PriceRule, rule_id) if rule_id else None
    if rule_id and (old_rule is None or not old_rule.active or old_rule.room_type_id != room_type_id):
        raise ManagerRejected("This price rule was already replaced or retired.")
    start, end, kind = form.start_date.data, form.end_date.data, form.kind.data
    if end <= start:
        raise ManagerRejected("The end date must follow the start date.")
    overlap = select(PriceRule.id).where(
        PriceRule.room_type_id == room_type_id, PriceRule.active.is_(True), PriceRule.kind == kind,
        PriceRule.start_date < end, PriceRule.end_date > start,
    )
    if rule_id:
        overlap = overlap.where(PriceRule.id != rule_id)
    if session.scalar(overlap.limit(1)) is not None:
        raise ManagerRejected("This date range overlaps a rule of the same priority for this room type.")
    fields = ("kind", "start_date", "end_date", "amount")
    before = audit_values(old_rule, fields) if old_rule else None
    if old_rule:
        old_rule.active = False
    rule = PriceRule(room_type_id=room_type_id, kind=kind, start_date=start, end_date=end, amount=form.amount.data)
    session.add(rule)
    session.flush()
    record_audit(
        session, actor_id, "replace_price_rule" if old_rule else "create_price_rule", "price_rule", rule.id,
        details={"previous_rule_id": rule_id, "before": before, "after": audit_values(rule, fields)},
    )


def retire_price_rule(session, actor_id, room_type_id, rule_id):
    locked_row(session, RoomType, room_type_id)
    rule = session.get(PriceRule, rule_id)
    if rule is None or rule.room_type_id != room_type_id:
        raise ManagerRejected("This price rule could not be found.", 404)
    if rule.active:
        rule.active = False
        record_audit(
            session, actor_id, "retire_price_rule", "price_rule", rule.id,
            details={"before": {"active": True}, "after": {"active": False}},
        )


def create_staff(session, actor_id, email, password):
    user = User(email=email, password_hash=generate_password_hash(password), role=STAFF)
    session.add(user)
    session.flush()
    record_audit(session, actor_id, "create_staff", "user", user.id, details={"email": email, "active": True})


def disable_staff(session, actor_id, staff_id):
    staff = locked_row(session, User, staff_id)
    if staff.role != STAFF:
        raise ManagerRejected("This staff account could not be found.", 404)
    if staff.active:
        staff.active = False
        record_audit(
            session, actor_id, "disable_staff", "user", staff.id,
            details={"before": {"active": True}, "after": {"active": False}},
        )


def change_password(session, actor_id, current_password, new_password):
    actor = locked_row(session, User, actor_id)
    if not check_password_hash(actor.password_hash, current_password):
        raise ManagerRejected("The current password is incorrect.", 400)
    actor.password_hash = generate_password_hash(new_password)
    record_audit(session, actor_id, "change_password", "user", actor.id)


def add_blacklist(session, actor_id, customer_id, reason):
    customer = locked_row(session, Customer, customer_id)
    at = utc_now()
    if customer_is_blocked(session, customer.id, at):
        raise ManagerRejected("This customer is already on the blacklist.")
    entry = BlacklistEntry(customer_id=customer.id, actor_user_id=actor_id, reason=reason, effective_at=at)
    session.add(entry)
    session.flush()
    record_audit(session, actor_id, "add_blacklist", "customer", customer.id, reason=reason,
                 details={"blacklist_entry_id": entry.id})


def remove_blacklist(session, actor_id, customer_id, reason):
    locked_row(session, Customer, customer_id)
    at = utc_now()
    entry = session.scalar(
        select(BlacklistEntry).where(
            BlacklistEntry.customer_id == customer_id, BlacklistEntry.effective_at <= at,
            or_(BlacklistEntry.removed_at.is_(None), BlacklistEntry.removed_at > at),
        ).order_by(BlacklistEntry.id.desc()).limit(1)
    )
    if entry is not None:
        entry.removed_at = at
        record_audit(session, actor_id, "remove_blacklist", "customer", customer_id, reason=reason,
                     details={"blacklist_entry_id": entry.id})
