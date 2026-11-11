"""Internal allocation service used by the pre-increment-2 integration tests."""

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from time import sleep
from uuid import UUID

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .audit import record_audit
from .blacklist import customer_is_blocked
from .catalog import free_room_conditions, hotel_now, nightly_rates, stay_error, total_for_rates
from .extensions import db
from .models import Booking, BookingPriceLine, Customer, GUEST, Room, RoomNight, RoomType, User, utc_now


class BookingRejected(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BookingResult:
    id: int
    total: str


def booking_result(booking, digest):
    if booking.request_digest != digest:
        raise BookingRejected("IDEMPOTENCY_MISMATCH", "The key belongs to a different request.")
    return BookingResult(booking.id, format(booking.total, ".2f"))


def existing_booking(session, actor_id, key):
    return session.scalar(
        db.select(Booking).where(Booking.actor_user_id == actor_id, Booking.idempotency_key == key)
    )


def add_night(session, booking, day, rate):
    session.add_all([
        RoomNight(room_id=booking.room_id, stay_date=day, booking_id=booking.id),
        BookingPriceLine(booking_id=booking.id, stay_date=day, amount=rate.amount, price_rule_id=rate.rule_id),
    ])


def allocate_once(session, actor_id, room_type_id, stay, key, digest):
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
        room = session.get(Room, room_id, with_for_update=True)
        existing = existing_booking(session, actor_id, key)
        if existing:
            return booking_result(existing, digest)
        if not room or room.room_type_id != room_type_id:
            continue
        available = session.scalar(db.select(Room.id).where(Room.id == room_id, *free_room_conditions(stay)))
        if available is not None:
            rates = nightly_rates(session, room_type, stay)
            booking = Booking(
                actor_user_id=actor_id, customer_id=customer.id, room_type_id=room_type_id,
                room_id=room_id, check_in=stay.check_in, check_out=stay.check_out,
                guests=stay.guests, total=total_for_rates(rates), idempotency_key=key, request_digest=digest,
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


def create_booking(actor_id, room_type_id, stay, idempotency_key):
    """Commit a testable guest allocation and return an immutable result."""
    error = stay_error(stay, hotel_now())
    if error:
        raise BookingRejected("INVALID_REQUEST", error)
    try:
        key = str(UUID(idempotency_key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BookingRejected("INVALID_REQUEST", "A UUID request key is required.") from exc
    digest = sha256(f"{room_type_id}|{stay.check_in}|{stay.check_out}|{stay.guests}".encode()).hexdigest()
    engine = db.engine.execution_options(isolation_level="READ COMMITTED")
    for attempt in range(3):
        try:
            with Session(engine) as session, session.begin():
                result = allocate_once(session, actor_id, room_type_id, stay, key, digest)
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
    raise BookingRejected("BUSY_RETRY", "The allocation is busy. Retry with the same key.")
