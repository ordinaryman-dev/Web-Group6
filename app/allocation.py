"""Shared automatic room selection. The caller owns the transaction and retries."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import lazyload

from .models import Booking, Room, RoomBlock, RoomNight


HISTORY_DAYS = 30


class AllocationRetry(Exception):
    """The candidate changed while waiting. Roll back before selecting again."""


@dataclass(frozen=True)
class RoomCandidate:
    room_id: int
    past_nights: int
    future_nights: int
    last_allocated_at: datetime | None

    @property
    def load(self):
        return self.past_nights + self.future_nights


def free_room_conditions(stay, today, *, ignore_booking_id=None):
    booked = select(RoomNight.id).where(
        RoomNight.room_id == Room.id,
        RoomNight.stay_date >= stay.check_in,
        RoomNight.stay_date < stay.check_out,
    )
    overdue = select(Booking.id).where(
        Booking.room_id == Room.id, Booking.status == "checked_in",
        Booking.check_out < stay.check_in,
    )
    if ignore_booking_id is not None:
        booked = booked.where(RoomNight.booking_id != ignore_booking_id)
        overdue = overdue.where(Booking.id != ignore_booking_id)
    blocked = exists().where(
        RoomBlock.room_id == Room.id,
        RoomBlock.effective.is_(True),
        RoomBlock.start_date < stay.check_out,
        RoomBlock.end_date > stay.check_in,
    )
    conditions = [
        Room.operational_from <= stay.check_in,
        or_(Room.operational_until.is_(None), Room.operational_until >= stay.check_out),
        ~booked.exists(), ~blocked, ~overdue.exists(),
    ]
    if stay.check_in <= today:
        conditions.append(Room.housekeeping == "clean")
    return conditions


def ranked_candidates(session, room_type_id, stay, today, *, ignore_booking_id=None, exclude_room_ids=(), clean_only=False):
    """Count retained nights, excluding unfulfilled history and inactive bookings.

    Past nights use [today - 30, today) and actual Beijing check-in/out dates.
    Future nights use [today, infinity) for confirmed / checked-in orders.
    These disjoint ranges prevent counting a night twice.
    """
    arrived = func.date(func.convert_tz(Booking.checked_in_at, "+00:00", "+08:00"))
    departed = func.date(func.convert_tz(Booking.checked_out_at, "+00:00", "+08:00"))
    past = (
        (RoomNight.stay_date < today)
        & Booking.status.in_(("checked_in", "checked_out"))
        & Booking.checked_in_at.is_not(None)
        & (RoomNight.stay_date >= arrived)
        & or_(Booking.checked_out_at.is_(None), RoomNight.stay_date < departed)
    )
    future = (RoomNight.stay_date >= today) & Booking.status.in_(("confirmed", "checked_in"))
    workload = select(
        RoomNight.room_id,
        func.sum(case((past, 1), else_=0)).label("past"),
        func.sum(case((future, 1), else_=0)).label("future"),
    ).join(Booking, Booking.id == RoomNight.booking_id).join(Room, Room.id == RoomNight.room_id).where(
        Room.room_type_id == room_type_id,
        RoomNight.stay_date >= today - timedelta(days=HISTORY_DAYS),
        or_(past, future),
    )
    if ignore_booking_id is not None:
        workload = workload.where(Booking.id != ignore_booking_id)
    workload = workload.group_by(RoomNight.room_id).subquery()
    past_count = func.coalesce(workload.c.past, 0)
    future_count = func.coalesce(workload.c.future, 0)
    statement = select(Room.id, past_count, future_count, Room.last_allocated_at).outerjoin(
        workload, workload.c.room_id == Room.id,
    ).where(
        Room.room_type_id == room_type_id,
        *free_room_conditions(stay, today, ignore_booking_id=ignore_booking_id),
    )
    if exclude_room_ids:
        statement = statement.where(Room.id.not_in(exclude_room_ids))
    if clean_only:
        statement = statement.where(Room.housekeeping == "clean")
    rows = session.execute(statement.order_by(
        past_count + future_count,
        Room.last_allocated_at.is_not(None), Room.last_allocated_at, Room.id,
    )).all()
    return [RoomCandidate(*row) for row in rows]


def select_room(session, room_type_id, stay, today, *, ignore_booking_id=None, exclude_room_ids=(),
                clean_only=False, original_room_id=None, requested_room_id=None):
    """Lock only the chosen target, plus an original room during reassignment.

    A stale or newly lower-ranked candidate aborts this whole attempt. The caller
    must retry with a fresh Session; it must not retain that lock and try another
    candidate. Two-room changes take Room locks in ascending primary-key order.
    """
    options = dict(
        ignore_booking_id=ignore_booking_id, exclude_room_ids=exclude_room_ids, clean_only=clean_only,
    )
    candidates = ranked_candidates(session, room_type_id, stay, today, **options)
    if requested_room_id is not None:
        candidates = [candidate for candidate in candidates if candidate.room_id == requested_room_id]
    if not candidates:
        return None
    # C07: when the room type did not change, the original room wins whenever it
    # still covers the whole stay. Load only decides between other rooms.
    prefer_original = original_room_id is not None and requested_room_id is None
    chosen = candidates[0].room_id
    if prefer_original:
        chosen = next(
            (candidate.room_id for candidate in candidates
             if candidate.room_id == original_room_id),
            chosen,
        )
    locks = {chosen}
    if original_room_id is not None:
        locks.add(original_room_id)
    for room_id in sorted(locks):
        session.get(Room, room_id, options=(lazyload("*"),),
                    with_for_update=True, populate_existing=True)
    current = ranked_candidates(session, room_type_id, stay, today, **options)
    if requested_room_id is not None:
        current = [candidate for candidate in current if candidate.room_id == requested_room_id]
    if not current:
        raise AllocationRetry()
    current_chosen = current[0].room_id
    if prefer_original:
        current_chosen = next(
            (candidate.room_id for candidate in current
             if candidate.room_id == original_room_id),
            current_chosen,
        )
    if current_chosen != chosen:
        raise AllocationRetry()
    return session.get(Room, chosen)


def record_assignment(room):
    # Microseconds distinguish successive successful assignments within one second.
    # This UPDATE is committed or rolled back with the booking and its RoomNight rows.
    room.last_allocated_at = datetime.now(timezone.utc).replace(tzinfo=None)
