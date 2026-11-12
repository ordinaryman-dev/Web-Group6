"""Front-desk operations: arrivals, check-in, check-out, payments, blocks, requests.

Every write runs in the caller's transaction, so a rule failure leaves the
booking, its room nights, its payments and its requests exactly as they were.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, lazyload

from .audit import record_audit
from .booking import (
    Stay,
    add_night,
    cancellation_deadline,
    free_room_conditions,
    hotel_now,
    nightly_rates,
    refundable_amount,
    total_for_rates,
)
from .extensions import db
from .models import (
    Booking,
    BookingPriceLine,
    BookingRequest,
    Customer,
    PaymentRecord,
    Room,
    RoomBlock,
    RoomNight,
    RoomType,
    utc_now,
)


CONFIRMED = "confirmed"
CHECKED_IN = "checked_in"
CHECKED_OUT = "checked_out"
CANCELLED = "cancelled"


class DeskError(Exception):
    """A rule the desk must explain to the receptionist."""


def paid_total(session, booking_id):
    return refundable_amount(session, booking_id)


def record_payment(session, booking, actor_id, amount=None):
    if booking.status in (CANCELLED, CHECKED_OUT):
        raise DeskError("This booking can no longer take a payment.")
    due = booking.total - paid_total(session, booking.id)
    if due <= 0:
        raise DeskError("This booking is already paid in full.")
    amount = due if amount is None else Decimal(amount)
    if amount <= 0:
        raise DeskError("Enter a payment greater than zero.")
    if amount > due:
        raise DeskError(f"Only ¥{due:,.2f} is outstanding on this booking.")
    payment = PaymentRecord(
        booking_id=booking.id,
        kind="payment",
        amount=amount,
        actor_user_id=actor_id,
        result="success",
        reference=f"pay-{uuid4()}",
    )
    session.add(payment)
    record_audit(session, actor_id, "record_payment", "booking", booking.id,
                 details={"amount": format(amount, ".2f")})
    return payment


def check_in_issues(session, booking, now):
    """The four checks the panel shows, as (label, ok, detail)."""
    paid = paid_total(session, booking.id)
    blocked = session.scalar(
        select(RoomBlock.id)
        .where(
            RoomBlock.room_id == booking.room_id,
            RoomBlock.effective.is_(True),
            RoomBlock.start_date < booking.check_out,
            RoomBlock.end_date > booking.check_in,
        )
        .limit(1)
    )
    return [
        ("Booking is confirmed", booking.status == CONFIRMED,
         f"Status: {booking.status.replace('_', ' ')}"),
        ("Arrival date is today", booking.check_in == now.date(),
         f"Arrival {booking.check_in:%d %b %Y}"),
        ("Room has no maintenance block for this stay", blocked is None,
         f"Room {booking.room.number}" if blocked is None else "A block covers this stay"),
        ("Simulated payment recorded", paid >= booking.total,
         f"Paid ¥{paid:,.2f} of ¥{booking.total:,.2f}"),
    ]


def check_in(session, booking_id, actor_id, now):
    booking = session.get(
        Booking, booking_id, options=(lazyload("*"),), with_for_update=True
    )
    if booking is None:
        raise DeskError("This booking could not be found.")
    if booking.status == CHECKED_IN:
        raise DeskError("This booking is already checked in.")
    if booking.status != CONFIRMED:
        raise DeskError("Only a confirmed booking can be checked in.")
    failed = [issue for issue in check_in_issues(session, booking, now) if not issue[1]]
    if failed:
        label, _, detail = failed[0]
        raise DeskError(f"{label}: {detail}.")
    booking.status = CHECKED_IN
    booking.checked_in_at = utc_now()
    record_audit(session, actor_id, "check_in", "booking", booking.id,
                 details={"room": booking.room.number,
                          "arrival": booking.check_in.isoformat()})
    return booking


def check_out(session, booking_id, actor_id, now):
    booking = session.get(
        Booking, booking_id, options=(lazyload("*"),), with_for_update=True
    )
    if booking is None:
        raise DeskError("This booking could not be found.")
    if booking.status != CHECKED_IN:
        raise DeskError("Only an in-house booking can be checked out.")
    booking.status = CHECKED_OUT
    booking.checked_out_at = utc_now()
    record_audit(session, actor_id, "check_out", "booking", booking.id,
                 details={"room": booking.room.number,
                          "departure": booking.check_out.isoformat()})
    return booking


def desk_today(session, now):
    today = now.date()
    arrivals = session.scalars(
        select(Booking)
        .where(Booking.check_in == today, Booking.status == CONFIRMED,
               Booking.checked_in_at.is_(None))
        .order_by(Booking.expected_arrival.is_(None), Booking.expected_arrival, Booking.id)
    ).all()
    in_house = session.scalars(
        select(Booking).where(Booking.status == CHECKED_IN).order_by(Booking.room_id)
    ).all()
    departures = session.scalars(
        select(Booking)
        .where(Booking.status == CHECKED_IN, Booking.check_out == today)
        .order_by(Booking.id)
    ).all()
    return arrivals, in_house, departures


def room_board(session, start, days=4):
    end = start + timedelta(days=days)
    rooms = session.scalars(select(Room).order_by(Room.number)).all()
    nights = session.execute(
        select(RoomNight.room_id, RoomNight.stay_date, Booking)
        .join(Booking, Booking.id == RoomNight.booking_id)
        .where(RoomNight.stay_date >= start, RoomNight.stay_date < end,
               Booking.status.in_((CONFIRMED, CHECKED_IN)))
    ).all()
    blocks = session.scalars(
        select(RoomBlock).where(RoomBlock.effective.is_(True),
                                RoomBlock.start_date < end, RoomBlock.end_date > start)
    ).all()
    by_cell = {(room_id, stay): booking for room_id, stay, booking in nights}
    leaving = departing_rooms(session, start)
    board = []
    for room in rooms:
        cells = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            booking = by_cell.get((room.id, day))
            blocked = any(
                block.room_id == room.id and block.start_date <= day < block.end_date
                for block in blocks
            )
            if blocked:
                state = "blocked"
            elif booking is not None:
                # A night belongs to a checked-in guest once they have arrived, so
                # the arrival day flips to "in house" instead of staying "arriving".
                # Future nights of a booking that has not arrived are "reserved".
                if booking.status == CHECKED_IN:
                    state = "in_house"
                elif booking.check_in == day:
                    state = "arriving"
                else:
                    state = "reserved"
            elif offset == 0 and room.id in leaving:
                state = "departing"
            else:
                state = "free"
            cells.append({"date": day, "state": state})
        board.append({"room": room, "cells": cells})
    return board


def departing_rooms(session, day):
    return {
        booking.room_id: booking
        for booking in session.scalars(
            select(Booking).where(Booking.status == CHECKED_IN, Booking.check_out == day)
        ).all()
    }


def pending_requests(session):
    return session.scalars(
        select(BookingRequest)
        .where(BookingRequest.status == "pending")
        .order_by(BookingRequest.submitted_at, BookingRequest.id)
    ).all()


def create_block(session, room_id, start, end, reason, actor_id):
    if end <= start:
        raise DeskError("The block must end after it starts.")
    reason = (reason or "").strip()
    if not reason:
        raise DeskError("Give a reason for the block.")
    room = session.get(Room, room_id)
    if room is None:
        raise DeskError("Choose a room.")
    block = RoomBlock(room_id=room.id, start_date=start, end_date=end, reason=reason)
    session.add(block)
    session.flush()
    conflicts = session.scalars(
        select(Booking).where(
            Booking.room_id == room.id,
            Booking.status.in_((CONFIRMED, CHECKED_IN)),
            Booking.check_in < end,
            Booking.check_out > start,
        )
    ).all()
    record_audit(session, actor_id, "create_block", "room_block", block.id, reason=reason,
                 details={"room": room.number, "from": start.isoformat(), "to": end.isoformat(),
                          "conflicts": [conflict.id for conflict in conflicts]})
    return block, conflicts


def cancel_block(session, block_id, actor_id, reason=None):
    """Release a maintenance block; the room becomes sellable again."""
    block = session.get(RoomBlock, block_id, with_for_update=True)
    if block is None:
        raise DeskError("This block could not be found.")
    if not block.effective:
        raise DeskError("This block is already cancelled.")
    block.effective = False
    record_audit(session, actor_id, "cancel_block", "room_block", block.id,
                 reason=(reason or "").strip() or "Desk released the room",
                 details={"room": block.room.number,
                          "from": block.start_date.isoformat(),
                          "to": block.end_date.isoformat()})
    return block

def free_rooms(session, room_type_id, stay):
    return session.scalars(
        select(Room.id)
        .where(Room.room_type_id == room_type_id, *free_room_conditions(stay))
        .order_by(Room.id)
    ).all()


def reassign(session, booking_id, actor_id, reason, room_type_id=None):
    booking = session.get(Booking, booking_id, options=(lazyload("*"),), with_for_update=True)
    if booking is None:
        raise DeskError("This booking could not be found.")
    if booking.status not in (CONFIRMED, CHECKED_IN):
        raise DeskError("Only an active booking can be reassigned.")
    reason = (reason or "").strip()
    if not reason:
        raise DeskError("Give a reason for the reassignment.")
    stay = Stay(booking.check_in, booking.check_out, booking.guests)
    old_room = booking.room.number
    # Release the held nights inside the transaction before looking for a replacement.
    session.execute(db.delete(RoomNight).where(RoomNight.booking_id == booking.id))
    session.flush()
    # The released room is free again, so drop it from the candidates: a reassignment
    # has to move the guest somewhere else, not back into the same room.
    candidates = [
        room_id for room_id in free_rooms(session, room_type_id or booking.room_type_id, stay)
        if room_id != booking.room_id
    ]
    if not candidates:
        raise DeskError("No other room of this type covers the stay. Ask the manager to cancel.")
    booking.room_id = candidates[0]
    booking.room = session.get(Room, candidates[0])
    for night in range(booking.nights):
        session.add(RoomNight(room_id=candidates[0],
                              stay_date=booking.check_in + timedelta(days=night),
                              booking_id=booking.id))
    session.flush()
    record_audit(session, actor_id, "reassign", "booking", booking.id, reason=reason,
                 details={"from": old_room,
                          "to": session.get(Room, candidates[0]).number})
    return booking


def _reprice(session, booking, stay):
    session.execute(db.delete(BookingPriceLine).where(BookingPriceLine.booking_id == booking.id))
    session.flush()
    rates = nightly_rates(session, booking.room_type, stay)
    for rate in rates:
        day = rate.start
        while day < rate.end:
            session.add(BookingPriceLine(booking_id=booking.id, stay_date=day,
                                         amount=rate.amount, price_rule_id=rate.rule_id))
            day += timedelta(days=1)
    booking.total = total_for_rates(rates)


def _rehouse(session, booking, stay, room_type_id):
    session.execute(db.delete(RoomNight).where(RoomNight.booking_id == booking.id))
    session.flush()
    candidates = free_rooms(session, room_type_id, stay)
    if not candidates:
        raise DeskError("No room covers the requested stay. The booking is unchanged.")
    booking.room_id = candidates[0]
    booking.check_in, booking.check_out = stay.check_in, stay.check_out
    booking.room_type_id = room_type_id
    booking.room_type = session.get(RoomType, room_type_id)
    for night in range((stay.check_out - stay.check_in).days):
        session.add(RoomNight(room_id=candidates[0],
                              stay_date=stay.check_in + timedelta(days=night),
                              booking_id=booking.id))
    session.flush()
    _reprice(session, booking, stay)


def approve_request(session, request_id, actor_id, comment=None):
    row = session.get(BookingRequest, request_id, with_for_update=True)
    if row is None:
        raise DeskError("This request could not be found.")
    if row.status != "pending":
        raise DeskError("This request has already been handled.")
    booking = session.get(Booking, row.booking_id, options=(lazyload("*"),), with_for_update=True)
    if booking.status in (CANCELLED, CHECKED_OUT):
        raise DeskError("The booking is no longer active.")
    target = row.target or {}
    if row.kind == "cancel":
        if booking.status != CONFIRMED or booking.checked_in_at is not None:
            raise DeskError("Only a confirmed, not yet checked-in booking can be cancelled.")
        # A request submitted after the deadline is a manager decision, not a desk one.
        if row.submitted_at and row.submitted_at > cancellation_deadline(booking):
            raise DeskError("This cancellation arrived after the deadline; only a manager can approve it.")
        refund = refundable_amount(session, booking.id)
        if refund > 0:
            session.add(PaymentRecord(
                booking_id=booking.id, kind="refund", amount=refund, actor_user_id=actor_id,
                result="success", reference=f"refund-{uuid4()}", request_id=row.id,
            ))
        session.execute(db.delete(RoomNight).where(RoomNight.booking_id == booking.id))
        booking.status = CANCELLED
        record_audit(session, actor_id, "cancel_by_request", "booking", booking.id,
                     reason=row.reason, details={"request_id": row.id,
                                                 "refund": format(refund, ".2f")})
    elif row.kind == "dates":
        stay = Stay(target.get("check_in"), target.get("check_out"), booking.guests)
        if not stay.check_in or not stay.check_out or stay.check_out <= stay.check_in:
            raise DeskError("The requested dates are invalid.")
        _rehouse(session, booking, stay, booking.room_type_id)
        record_audit(session, actor_id, "apply_request", "booking", booking.id,
                     reason=row.reason, details={"request_id": row.id, "kind": "dates",
                                                 "check_in": stay.check_in.isoformat(),
                                                 "check_out": stay.check_out.isoformat()})
    elif row.kind == "room_type":
        room_type_id = target.get("room_type_id")
        if not room_type_id or session.get(RoomType, room_type_id) is None:
            raise DeskError("The requested room type is unavailable.")
        stay = Stay(booking.check_in, booking.check_out, booking.guests)
        _rehouse(session, booking, stay, room_type_id)
        record_audit(session, actor_id, "apply_request", "booking", booking.id,
                     reason=row.reason, details={"request_id": row.id, "kind": "room_type",
                                                 "room_type_id": room_type_id})
    else:
        raise DeskError("This request type cannot be approved here.")
    row.status = "approved"
    row.handled_by = actor_id
    row.handled_at = utc_now()
    row.comment = comment
    return row


def reject_request(session, request_id, actor_id, comment=None):
    row = session.get(BookingRequest, request_id, with_for_update=True)
    if row is None:
        raise DeskError("This request could not be found.")
    if row.status != "pending":
        raise DeskError("This request has already been handled.")
    row.status = "rejected"
    row.handled_by = actor_id
    row.handled_at = utc_now()
    row.comment = comment
    record_audit(session, actor_id, "reject_request", "booking_request", row.id,
                 reason=comment or row.reason)
    return row


def create_walkin(session, actor_id, name, phone, room_type_id, stay, note=None):
    """Open a room for a guest at the desk; same availability and price rules."""
    name = (name or "").strip()
    phone = (phone or "").strip()
    if not name:
        raise DeskError("Enter the guest's name.")
    if not phone:
        raise DeskError("Enter a contact phone for the walk-in.")
    room_type = session.get(RoomType, room_type_id)
    if room_type is None or not room_type.listed:
        raise DeskError("Choose a listed room type.")
    if room_type.capacity < stay.guests:
        raise DeskError("The room type is too small for that party.")
    customer = Customer(name=name, phone=phone)
    session.add(customer)
    session.flush()
    candidates = free_rooms(session, room_type_id, stay)
    if not candidates:
        raise DeskError("No room covers the whole stay. Nothing was booked.")
    rates = nightly_rates(session, room_type, stay)
    booking = Booking(
        customer_id=customer.id,
        room_id=candidates[0],
        room_type_id=room_type_id,
        actor_user_id=actor_id,
        idempotency_key=f"walkin-{uuid4()}",
        request_digest=f"walkin-{name}-{stay.check_in}-{stay.check_out}",
        check_in=stay.check_in,
        check_out=stay.check_out,
        guests=stay.guests,
        total=total_for_rates(rates),
        note=(note or "").strip() or None,
        status=CONFIRMED,
    )
    session.add(booking)
    session.flush()
    day = stay.check_in
    while day < stay.check_out:
        rate = next(rate for rate in rates if rate.start <= day < rate.end)
        add_night(session, booking, day, rate)
        day += timedelta(days=1)
    record_audit(session, actor_id, "create_booking", "booking", booking.id,
                 details={"source": "front_desk_walk_in", "customer": name})
    return booking


def deadline_passed(booking, now):
    return now.replace(tzinfo=None) > cancellation_deadline(booking)
