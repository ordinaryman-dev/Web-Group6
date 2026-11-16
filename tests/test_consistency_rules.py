"""Rules that keep the desk honest under pressure.

1. Two Collect/Refund clicks cannot record the same money twice.
2. A no-show needs a contact record first, not just a late clock.
3. A room that is still dirty cannot be sold for tonight.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import desk as desk_service
from app.booking import Stay, free_room_conditions
from app.extensions import db
from app.models import Booking, PaymentRecord, Room
from test_frontdesk_rules import make_booking, make_type_and_rooms, staff_id

TODAY = date(2030, 6, 18)
LATE = datetime(2030, 6, 18, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def payment_total(app, booking_id):
    with app.app_context():
        rows = db.session.scalars(
            select(PaymentRecord).where(PaymentRecord.booking_id == booking_id)
        ).all()
        return sum(
            row.amount if row.kind == "payment" else -row.amount for row in rows
        )


def test_two_collect_clicks_record_one_payment(app):
    type_id, rooms = make_type_and_rooms(app, "p0-pay", count=1)
    booking_id = make_booking(app, type_id, rooms, nights=1)  # total 200.00
    actor = staff_id(app)
    with app.app_context():
        engine = db.engine.execution_options(isolation_level="READ COMMITTED")
    barrier = Barrier(2)

    def collect():
        barrier.wait(timeout=10)
        try:
            with Session(engine) as session, session.begin():
                desk_service.record_payment(
                    session, session.get(Booking, booking_id), actor, Decimal("200.00")
                )
            return "paid"
        except desk_service.DeskError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: collect(), range(2)))

    assert sorted(outcomes) == ["paid", "refused"]
    assert payment_total(app, booking_id) == Decimal("200.00")


def test_two_refund_clicks_cannot_refund_more_than_collected(app):
    type_id, rooms = make_type_and_rooms(app, "p0-refund", count=1)
    booking_id = make_booking(app, type_id, rooms, nights=1, status="cancelled",
                              paid=Decimal("200.00"))
    actor = staff_id(app)
    with app.app_context():
        engine = db.engine.execution_options(isolation_level="READ COMMITTED")
    barrier = Barrier(2)

    def give_back():
        barrier.wait(timeout=10)
        try:
            with Session(engine) as session, session.begin():
                desk_service.record_refund(
                    session, session.get(Booking, booking_id), actor, Decimal("200.00")
                )
            return "refunded"
        except desk_service.DeskError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: give_back(), range(2)))

    assert sorted(outcomes) == ["refunded", "refused"]
    assert payment_total(app, booking_id) == Decimal("0.00")


def test_a_no_show_needs_a_contact_record_first(app):
    type_id, rooms = make_type_and_rooms(app, "p0-noshow")
    booking_id = make_booking(app, type_id, rooms, nights=2, expected=time(18, 0))
    actor = staff_id(app)

    with app.app_context():
        with pytest.raises(desk_service.DeskError) as error:
            desk_service.mark_no_show(db.session, booking_id, actor, "no answer", LATE)
        assert "Record the contact result" in str(error.value)
        assert db.session.get(Booking, booking_id).status == "confirmed"

    with app.app_context():
        desk_service.contact_customer(db.session, booking_id, actor, "no_answer",
                                      "rang twice", time(20, 30))
        db.session.commit()
    with app.app_context():
        desk_service.mark_no_show(db.session, booking_id, actor, "unreachable", LATE)
        db.session.commit()
        assert db.session.get(Booking, booking_id).status == "no_show"


def test_a_dirty_room_is_not_sellable_for_tonight(app):
    type_id, rooms = make_type_and_rooms(app, "p0-dirty", count=1)
    with app.app_context():
        room = db.session.get(Room, rooms[0])
        room.housekeeping = "dirty"
        db.session.commit()

    tonight = Stay(TODAY, TODAY + timedelta(days=1), 2)
    later = Stay(TODAY + timedelta(days=3), TODAY + timedelta(days=4), 2)
    with app.app_context():
        free_tonight = db.session.scalar(
            select(Room.id).where(Room.id == rooms[0], *free_room_conditions(tonight))
        )
        free_later = db.session.scalar(
            select(Room.id).where(Room.id == rooms[0], *free_room_conditions(later))
        )

    assert free_tonight is None
    assert free_later == rooms[0]
