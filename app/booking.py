from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from time import sleep
from uuid import UUID
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import exists, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .audit import record_audit
from .extensions import db
from .models import (
    BlacklistEntry,
    Booking,
    BookingPriceLine,
    Customer,
    GUEST,
    PriceRule,
    Room,
    RoomBlock,
    RoomNight,
    RoomType,
    User,
    utc_now,
)


HOTEL_ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Stay:
    check_in: date
    check_out: date
    guests: int


@dataclass(frozen=True)
class Rate:
    start: date
    end: date
    amount: Decimal
    rule_id: int | None


@dataclass(frozen=True)
class Quote:
    room_type: RoomType
    total: Decimal
    rates: list[Rate]


class BookingRejected(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BookingResult:
    id: int
    total: str


def hotel_now():
    clock = current_app.config.get("HOTEL_CLOCK")
    return clock().astimezone(HOTEL_ZONE) if clock else datetime.now(HOTEL_ZONE)


def stay_error(stay, now):
    today = now.date()
    if not today <= stay.check_in <= today + timedelta(days=14):
        return "Choose a check-in date from today through 14 days ahead."
    if stay.check_out <= stay.check_in:
        return "Check-out must be after check-in."
    if stay.check_in == today and now.time() >= time(22):
        return "Same-day bookings close at 22:00 Beijing time."
    if stay.guests < 1:
        return "Enter at least one guest."
    return None


def free_room_conditions(stay):
    booked = exists().where(
        RoomNight.room_id == Room.id,
        RoomNight.stay_date >= stay.check_in,
        RoomNight.stay_date < stay.check_out,
    )
    blocked = exists().where(
        RoomBlock.room_id == Room.id,
        RoomBlock.start_date < stay.check_out,
        RoomBlock.end_date > stay.check_in,
    )
    return (
        Room.operational_from <= stay.check_in,
        or_(Room.operational_until.is_(None), Room.operational_until >= stay.check_out),
        ~booked,
        ~blocked,
    )


def nightly_rates(session, room_type, stay):
    rules = session.scalars(
        db.select(PriceRule).where(
            PriceRule.room_type_id == room_type.id,
            PriceRule.start_date < stay.check_out,
            PriceRule.end_date > stay.check_in,
        )
    ).all()
    boundaries = {stay.check_in, stay.check_out}
    for rule in rules:
        boundaries.update((max(stay.check_in, rule.start_date), min(stay.check_out, rule.end_date)))
    days = sorted(boundaries)
    rates = []
    for start, end in zip(days, days[1:]):
        matches = [rule for rule in rules if rule.start_date <= start < rule.end_date]
        best = max(matches, key=lambda rule: (rule.kind == "date", rule.start_date, rule.id), default=None)
        amount = best.amount if best else room_type.base_price
        rule_id = best.id if best else None
        if rates and (rates[-1].amount, rates[-1].rule_id) == (amount, rule_id):
            rates[-1] = Rate(rates[-1].start, end, amount, rule_id)
        else:
            rates.append(Rate(start, end, amount, rule_id))
    return rates


def total_for_rates(rates):
    return sum((rate.amount * (rate.end - rate.start).days for rate in rates), Decimal("0.00"))


def search_results(session, stay):
    whole_stay_room = exists().where(Room.room_type_id == RoomType.id, *free_room_conditions(stay))
    room_types = session.scalars(
        db.select(RoomType)
        .where(RoomType.listed.is_(True), RoomType.capacity >= stay.guests, whole_stay_room)
        .order_by(RoomType.capacity, RoomType.base_price, RoomType.id)
    ).all()
    quotes = []
    for room_type in room_types:
        rates = nightly_rates(session, room_type, stay)
        quotes.append(Quote(room_type=room_type, total=total_for_rates(rates), rates=rates))
    return quotes


def listed_room_types(session):
    return session.scalars(
        db.select(RoomType)
        .where(RoomType.listed.is_(True))
        .order_by(RoomType.capacity, RoomType.base_price, RoomType.id)
    ).all()


def customer_is_blocked(session, customer_id, at):
    return session.scalar(
        db.select(BlacklistEntry.id)
        .where(
            BlacklistEntry.customer_id == customer_id,
            BlacklistEntry.effective_at <= at,
            or_(BlacklistEntry.removed_at.is_(None), BlacklistEntry.removed_at > at),
        )
        .limit(1)
    ) is not None


def existing_booking(session, actor_id, key):
    return session.scalar(
        db.select(Booking).where(Booking.actor_user_id == actor_id, Booking.idempotency_key == key)
    )


def booking_result(booking, digest):
    if booking.request_digest != digest:
        raise BookingRejected("IDEMPOTENCY_MISMATCH", "This request key belongs to a different booking.")
    return BookingResult(booking.id, format(booking.total, ".2f"))


def add_night(session, booking, day, rate):
    session.add_all([
        RoomNight(room_id=booking.room_id, stay_date=day, booking_id=booking.id),
        BookingPriceLine(
            booking_id=booking.id,
            stay_date=day,
            amount=rate.amount,
            price_rule_id=rate.rule_id,
        ),
    ])


def allocate_once(session, actor_id, room_type_id, stay, key, digest, note):
    actor = session.get(User, actor_id)
    if not actor or not actor.active or actor.role != GUEST:
        raise BookingRejected("FORBIDDEN", "An active guest account is required.")

    existing = existing_booking(session, actor_id, key)
    if existing:
        return booking_result(existing, digest)

    customer = session.scalar(
        db.select(Customer).where(Customer.user_id == actor_id).with_for_update(read=True)
    )
    if not customer or customer_is_blocked(session, customer.id, utc_now()):
        raise BookingRejected("FORBIDDEN", "This customer cannot make a new booking.")

    room_type = session.scalar(
        db.select(RoomType).where(RoomType.id == room_type_id).with_for_update(read=True)
    )
    if not room_type or not room_type.listed or room_type.capacity < stay.guests:
        raise BookingRejected("SOLD_OUT", "The room type is unavailable for this stay.")

    candidates = session.scalars(
        db.select(Room.id).where(Room.room_type_id == room_type_id).order_by(Room.id)
    ).all()
    for room_id in candidates:
        room = session.scalar(db.select(Room).where(Room.id == room_id).with_for_update())
        existing = existing_booking(session, actor_id, key)
        if existing:
            return booking_result(existing, digest)
        if not room or room.room_type_id != room_type_id:
            continue
        available = session.scalar(
            db.select(Room.id).where(Room.id == room_id, *free_room_conditions(stay))
        )
        if available is not None:
            rates = nightly_rates(session, room_type, stay)
            booking = Booking(
                actor_user_id=actor_id,
                customer_id=customer.id,
                room_type_id=room_type_id,
                room_id=room_id,
                check_in=stay.check_in,
                check_out=stay.check_out,
                guests=stay.guests,
                total=total_for_rates(rates),
                idempotency_key=key,
                request_digest=digest,
                note=note,
            )
            session.add(booking)
            session.flush()
            for rate in rates:
                day = rate.start
                while day < rate.end:
                    add_night(session, booking, day, rate)
                    day += timedelta(days=1)
            record_audit(session, actor_id, "create_booking", "booking", booking.id)
            return booking_result(booking, digest)

    raise BookingRejected("SOLD_OUT", "No single room covers the entire stay.")


def create_booking(actor_id, room_type_id, stay, idempotency_key, note=None):
    error = stay_error(stay, hotel_now())
    if error:
        raise BookingRejected("INVALID_REQUEST", error)

    note = (note or "").strip() or None
    if note is not None and len(note) > 100:
        raise BookingRejected("INVALID_REQUEST", "The note must be 100 characters or fewer.")

    try:
        key = str(UUID(str(idempotency_key)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BookingRejected("INVALID_REQUEST", "A request key is required.") from exc

    digest = sha256(
        f"{room_type_id}|{stay.check_in}|{stay.check_out}|{stay.guests}|{note or ''}".encode()
    ).hexdigest()
    engine = db.engine.execution_options(isolation_level="READ COMMITTED")

    for attempt in range(3):
        try:
            with Session(engine) as session, session.begin():
                result = allocate_once(session, actor_id, room_type_id, stay, key, digest, note)
        except IntegrityError as exc:
            if not exc.orig.args or exc.orig.args[0] != 1062:
                raise
            with Session(engine) as session:
                existing = existing_booking(session, actor_id, key)
                if existing:
                    return booking_result(existing, digest)
            if "uq_room_night" not in str(exc.orig):
                raise
        except OperationalError as exc:
            if not exc.orig.args or exc.orig.args[0] not in (1205, 1213):
                raise
        else:
            return result
        if attempt < 2:
            sleep(0.02 * (attempt + 1))

    raise BookingRejected("BUSY_RETRY", "The rooms are busy. Please try the same booking again.")


def get_booking(session, booking_id, user_id):
    return session.scalar(
        db.select(Booking).where(Booking.id == booking_id, Booking.actor_user_id == user_id)
    )


def recent_bookings(session, user_id, limit=3):
    return session.scalars(
        db.select(Booking)
        .where(Booking.actor_user_id == user_id)
        .order_by(Booking.created_at.desc(), Booking.id.desc())
        .limit(limit)
    ).all()
