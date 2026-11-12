"""Rule-level tests for the front-desk requirements E01–E08."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from app import desk as desk_service
from app.booking import Stay
from app.extensions import db
from app.models import (
    AuditLog,
    BlacklistEntry,
    Booking,
    BookingPriceLine,
    BookingRequest,
    Customer,
    GUEST,
    PaymentRecord,
    Room,
    RoomBlock,
    RoomNight,
    RoomType,
    STAFF,
    User,
    utc_now,
)


TODAY = date(2030, 6, 18)
NOW = datetime(2030, 6, 18, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
PASSWORD = "A password for integration tests"


def new_user(app, role):
    with app.app_context():
        user = User(email=f"{role}-{uuid4()}@example.test",
                    password_hash=generate_password_hash(PASSWORD), role=role)
        db.session.add(user)
        db.session.commit()
        return user.id


def make_type_and_rooms(app, label, count=2, price="200.00", capacity=2):
    with app.app_context():
        room_type = RoomType(code=label, name=label, description="test", capacity=capacity,
                             base_price=Decimal(price))
        db.session.add(room_type)
        db.session.flush()
        rooms = []
        for index in range(count):
            room = Room(number=f"{label}-{index}", room_type_id=room_type.id,
                        operational_from=date(2020, 1, 1))
            db.session.add(room)
            rooms.append(room)
        db.session.commit()
        return room_type.id, [room.id for room in rooms]


def make_booking(app, room_type_id, room_ids, *, check_in=TODAY, nights=2, guests=2,
                 status="confirmed", expected=time(18, 0), paid=None):
    with app.app_context():
        customer = Customer(name=f"Guest {uuid4().hex[:6]}", phone="13800000000")
        db.session.add(customer)
        db.session.flush()
        actor = staff_id(app)
        total = Decimal("200.00") * nights
        booking = Booking(
            customer_id=customer.id, room_id=room_ids[0], room_type_id=room_type_id,
            actor_user_id=actor, idempotency_key=f"t-{uuid4()}", request_digest="t",
            check_in=check_in, check_out=check_in + timedelta(days=nights), guests=guests,
            status=status, total=total, expected_arrival=expected,
            checked_in_at=utc_now() if status == "checked_in" else None,
        )
        db.session.add(booking)
        db.session.flush()
        for index in range(nights):
            day = check_in + timedelta(days=index)
            db.session.add(RoomNight(room_id=room_ids[0], stay_date=day, booking_id=booking.id))
            db.session.add(BookingPriceLine(booking_id=booking.id, stay_date=day,
                                            amount=Decimal("200.00")))
        if paid:
            db.session.add(PaymentRecord(booking_id=booking.id, kind="payment", amount=paid,
                                         actor_user_id=actor, result="success",
                                         reference=f"pay-{uuid4()}"))
        db.session.commit()
        return booking.id


def booking_row(app, booking_id):
    with app.app_context():
        booking = db.session.get(Booking, booking_id)
        return {
            "status": booking.status,
            "room": booking.room_id,
            "check_in": booking.check_in,
            "check_out": booking.check_out,
            "total": booking.total,
            "nights": db.session.query(RoomNight).filter_by(booking_id=booking.id).count(),
            "checked_in_at": booking.checked_in_at,
            "no_show_reason": booking.no_show_reason,
            "expected": booking.expected_arrival,
        }


def staff_id(app):
    with app.app_context():
        existing = db.session.scalar(select(User.id).where(User.role == STAFF).limit(1))
        if existing:
            return existing
        user = User(email=f"staff-{uuid4()}@example.test",
                    password_hash=generate_password_hash(PASSWORD), role=STAFF)
        db.session.add(user)
        db.session.commit()
        return user.id


def test_e01_board_filters_by_room_type_and_room_number(app):
    suite_id, suite_rooms = make_type_and_rooms(app, "e01-suite", count=2)
    twin_id, _ = make_type_and_rooms(app, "e01-twin", count=1)
    with app.app_context():
        board = desk_service.room_board(db.session, TODAY, 3, room_type_id=suite_id)
        assert {row["room"].id for row in board} == set(suite_rooms)
        assert all(len(row["cells"]) == 3 for row in board)

        one_room = desk_service.room_board(db.session, TODAY, 2, room_query="e01-suite-1")
        assert [row["room"].id for row in one_room] == [suite_rooms[1]]
        assert len(one_room[0]["cells"]) == 2


def test_e01_search_finds_a_booking_by_reference_and_name(app):
    from tests.test_frontdesk import post, sign_in  # reuse the helpers

    type_id, rooms = make_type_and_rooms(app, "e01-search")
    booking_id = make_booking(app, type_id, rooms)
    staff_email = f"staff-{uuid4()}@example.test"
    with app.app_context():
        db.session.add(User(email=staff_email,
                            password_hash=generate_password_hash(PASSWORD), role=STAFF))
        db.session.commit()
    staff = sign_in(app, staff_email)

    page = staff.get("/frontdesk", query_string={"q": f"BK-{booking_id:06d}"}).text
    assert f"BK-{booking_id:06d}" in page and "SEARCH RESULTS" in page

    with app.app_context():
        guest_name = db.session.get(Booking, booking_id).customer.name
    page = staff.get("/frontdesk", query_string={"q": guest_name}).text
    assert guest_name in page


def test_e02_check_in_waits_for_1400_and_records_the_real_time(app):
    type_id, rooms = make_type_and_rooms(app, "e02-window")
    booking_id = make_booking(app, type_id, rooms, paid=Decimal("400.00"))
    actor = staff_id(app)

    early = datetime(2030, 6, 18, 13, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    with app.app_context():
        with pytest.raises(desk_service.DeskError) as error:
            desk_service.check_in(db.session, booking_id, actor, early)
        assert "14:00" in str(error.value)
    assert booking_row(app, booking_id)["status"] == "confirmed"

    with app.app_context():
        desk_service.check_in(db.session, booking_id, actor, NOW)
        db.session.commit()
    row = booking_row(app, booking_id)
    assert row["status"] == "checked_in"
    assert row["checked_in_at"] is not None
    # The recorded time is the real one, not a fabricated 14:00.
    assert row["checked_in_at"] <= utc_now()


def test_e02_late_check_out_is_recorded_and_flagged(app):
    type_id, rooms = make_type_and_rooms(app, "e02-late")
    booking_id = make_booking(app, type_id, rooms, nights=1, status="checked_in")
    actor = staff_id(app)
    late = datetime(2030, 6, 19, 13, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    with app.app_context():
        outcome = desk_service.check_out(db.session, booking_id, actor, late)
        db.session.commit()
    assert outcome["late"] is True
    assert booking_row(app, booking_id)["status"] == "checked_out"


def test_e03_walkin_reuses_a_guest_and_refuses_a_blacklisted_guest(app):
    type_id, rooms = make_type_and_rooms(app, "e03-walkin", capacity=2)
    actor = staff_id(app)
    stay = Stay(TODAY, TODAY + timedelta(days=1), 2)

    with app.app_context():
        customer = Customer(name="Repeat Guest", phone="13700000000")
        db.session.add(customer)
        db.session.commit()
        customer_id = customer.id

    with app.app_context():
        booking = desk_service.create_walkin(db.session, actor, type_id, stay,
                                             customer_id=customer_id)
        db.session.commit()
        assert booking.customer_id == customer_id
        assert booking.status == "checked_in"
        assert db.session.query(PaymentRecord).filter_by(booking_id=booking.id).count() == 1
        assert db.session.query(Customer).filter_by(name="Repeat Guest").count() == 1

    with app.app_context():
        blocked = Customer(name="Blocked Guest", phone="13700000001")
        db.session.add(blocked)
        db.session.flush()
        db.session.add(BlacklistEntry(customer_id=blocked.id, actor_user_id=actor,
                                      reason="Damaged a room"))
        db.session.commit()
        blocked_id = blocked.id

    with app.app_context():
        with pytest.raises(desk_service.DeskError) as error:
            desk_service.create_walkin(db.session, actor, type_id, stay, customer_id=blocked_id)
        assert "blacklisted" in str(error.value)
        assert db.session.query(Booking).filter_by(customer_id=blocked_id).count() == 0


def test_e04_block_records_operator_and_keeps_existing_bookings(app):
    type_id, rooms = make_type_and_rooms(app, "e04-block")
    booking_id = make_booking(app, type_id, rooms, check_in=TODAY + timedelta(days=1))
    actor = staff_id(app)

    with app.app_context():
        block, conflicts = desk_service.create_block(
            db.session, rooms[0], TODAY + timedelta(days=1),
            TODAY + timedelta(days=2), "Repaint", actor)
        db.session.commit()
        assert block.created_by_id == actor
        assert [conflict.id for conflict in conflicts] == [booking_id]
    assert booking_row(app, booking_id)["status"] == "confirmed"


def test_e04_in_house_guests_are_not_reassigned(app):
    type_id, rooms = make_type_and_rooms(app, "e04-inhouse")
    booking_id = make_booking(app, type_id, rooms, status="checked_in")
    actor = staff_id(app)
    with app.app_context():
        with pytest.raises(desk_service.DeskError):
            desk_service.reassign(db.session, booking_id, actor, "move")
    assert booking_row(app, booking_id)["room"] == rooms[0]


def test_e05_the_same_request_can_only_be_decided_once(app):
    type_id, rooms = make_type_and_rooms(app, "e05-once")
    booking_id = make_booking(app, type_id, rooms, check_in=TODAY + timedelta(days=2))
    actor = staff_id(app)
    with app.app_context():
        request_row = BookingRequest(
            booking_id=booking_id, kind="dates",
            target={"check_in": str(TODAY + timedelta(days=3)),
                    "check_out": str(TODAY + timedelta(days=5))},
            reason="Shift the stay", submitted_by=actor, status="pending",
        )
        db.session.add(request_row)
        db.session.commit()
        request_id = request_row.id

    barrier = Barrier(2)
    with app.app_context():
        engine = db.engine.execution_options(isolation_level="READ COMMITTED")

    def decide():
        barrier.wait(timeout=10)
        try:
            with Session(engine) as session, session.begin():
                desk_service.approve_request(session, request_id, actor, "ok")
            return "approved"
        except desk_service.DeskError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: decide(), range(2)))

    assert sorted(outcomes) == ["approved", "rejected"]
    with app.app_context():
        row = db.session.get(BookingRequest, request_id)
        assert row.status == "approved" and row.comment == "ok"
        assert db.session.query(Booking).filter_by(id=booking_id).one().check_in == (
            TODAY + timedelta(days=3))


def test_e06_cancellation_releases_nights_but_keeps_the_record(app):
    type_id, rooms = make_type_and_rooms(app, "e06-cancel")
    booking_id = make_booking(app, type_id, rooms, check_in=TODAY + timedelta(days=3),
                              paid=Decimal("400.00"))
    actor = staff_id(app)
    with app.app_context():
        request_row = BookingRequest(booking_id=booking_id, kind="cancel", target={},
                                     reason="Plans changed", submitted_by=actor,
                                     status="pending", submitted_at=utc_now())
        db.session.add(request_row)
        db.session.commit()
        request_id = request_row.id

    with app.app_context():
        desk_service.approve_request(db.session, request_id, actor, "ok")
        db.session.commit()

    row = booking_row(app, booking_id)
    assert row["status"] == "cancelled"
    assert row["nights"] == 0
    with app.app_context():
        assert db.session.get(Booking, booking_id) is not None
        refund = db.session.scalar(select(PaymentRecord).where(
            PaymentRecord.booking_id == booking_id, PaymentRecord.kind == "refund"))
        assert refund is not None and refund.amount == Decimal("400.00")
        assert db.session.query(AuditLog).filter_by(action="cancel_by_request").count() == 1


def test_e07_payments_are_not_duplicated(app):
    type_id, rooms = make_type_and_rooms(app, "e07-pay")
    booking_id = make_booking(app, type_id, rooms, nights=1)
    actor = staff_id(app)
    with app.app_context():
        desk_service.record_payment(db.session, db.session.get(Booking, booking_id),
                                    actor, Decimal("200.00"))
        db.session.commit()
    with app.app_context():
        with pytest.raises(desk_service.DeskError) as error:
            desk_service.record_payment(db.session, db.session.get(Booking, booking_id),
                                        actor, Decimal("200.00"))
        assert "paid in full" in str(error.value)
        assert db.session.query(PaymentRecord).filter_by(booking_id=booking_id).count() == 1


def test_e08_contact_then_no_show_after_the_expected_time(app):
    type_id, rooms = make_type_and_rooms(app, "e08-noshow")
    booking_id = make_booking(app, type_id, rooms, nights=2, expected=time(18, 0),
                              paid=Decimal("400.00"))
    actor = staff_id(app)

    early = datetime(2030, 6, 18, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with app.app_context():
        with pytest.raises(desk_service.DeskError):
            desk_service.mark_no_show(db.session, booking_id, actor, "no answer", early)

    late = datetime(2030, 6, 18, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with app.app_context():
        desk_service.contact_customer(db.session, booking_id, actor, "no_answer",
                                      "rang twice", time(20, 30))
        db.session.commit()
    assert booking_row(app, booking_id)["expected"] == time(20, 30)

    with app.app_context():
        desk_service.mark_no_show(db.session, booking_id, actor, "unreachable", late)
        db.session.commit()

    row = booking_row(app, booking_id)
    assert row["status"] == "no_show"
    assert row["nights"] == 0
    assert row["no_show_reason"] == "unreachable"
    with app.app_context():
        assert db.session.query(PaymentRecord).filter_by(
            booking_id=booking_id, kind="refund").count() == 0
        assert db.session.query(AuditLog).filter_by(action="mark_no_show").count() == 1


def test_e08_no_show_refund_follows_the_manager_cancellation_rule(app):
    from app.booking import cancel_confirmed_booking

    type_id, rooms = make_type_and_rooms(app, "e08-refund")
    booking_id = make_booking(app, type_id, rooms, nights=1, expected=time(9, 0),
                              paid=Decimal("200.00"))
    actor = staff_id(app)
    late = datetime(2030, 6, 18, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with app.app_context():
        desk_service.mark_no_show(db.session, booking_id, actor, "unreachable", late)
        db.session.commit()

    manager_id = new_user(app, "manager")
    with app.app_context():
        refund = cancel_confirmed_booking(db.session, manager_id, booking_id, "hotel",
                                          "No-show goodwill refund", NOW)
        db.session.commit()
        assert refund == Decimal("200.00")
    row = booking_row(app, booking_id)
    assert row["status"] == "cancelled"
    with app.app_context():
        refunds = db.session.query(PaymentRecord).filter_by(
            booking_id=booking_id, kind="refund").all()
        payments = db.session.query(PaymentRecord).filter_by(
            booking_id=booking_id, kind="payment").all()
        assert sum(r.amount for r in refunds) <= sum(p.amount for p in payments)
