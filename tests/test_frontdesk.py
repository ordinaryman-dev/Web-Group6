import re
from uuid import uuid4
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import desk as desk_service
from app.extensions import db
from app.models import (
    AuditLog,
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
)
from werkzeug.security import generate_password_hash


TODAY = date(2030, 6, 18)
PASSWORD = "A password for integration tests"


def token(page):
    return re.search(r'name="[^"]*csrf_token"[^>]*value="([^"]+)"', page.text).group(1)


def post(client, path, data, *, form_path="/frontdesk"):
    page = client.get(form_path)
    return client.post(path, data={**data, "csrf_token": token(page)})


def new_user(app, role, *, email=None):
    with app.app_context():
        user = User(
            email=email or f"{role}-{uuid4()}@example.test",
            password_hash=generate_password_hash(PASSWORD),
            role=role,
        )
        db.session.add(user)
        if role == GUEST:
            db.session.add(Customer(user=user, name=f"Customer {user.email}", phone="13800000000"))
        db.session.commit()
        return user.id, user.email


def sign_in(app, email, password=PASSWORD):
    client = app.test_client()
    page = client.get("/login")
    response = client.post("/login", data={
        "email": email, "password": password, "csrf_token": token(page),
    })
    assert response.status_code == 302
    return client


def make_room(app, label, *, price="200.00"):
    with app.app_context():
        room_type = RoomType(code=label, name=label, description="Test room", capacity=2,
                             base_price=Decimal(price))
        db.session.add(room_type)
        db.session.flush()
        room = Room(number=label, room_type_id=room_type.id, operational_from=date(2020, 1, 1))
        db.session.add(room)
        db.session.commit()
        return room_type.id, room.id


def reserve(guest, room_type_id, check_in, nights=1):
    query = {"check_in": str(check_in), "check_out": str(check_in + timedelta(days=nights)),
             "guests": 2}
    page = guest.get("/guest/", query_string=query)
    assert page.status_code == 200
    for form in re.findall(r'<form method="post" action="/guest/book".*?</form>', page.text, re.S):
        fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]+)"', form))
        if fields["room_type_id"] == str(room_type_id):
            response = guest.post("/guest/book", data={**query, **fields, "note": ""})
            assert response.status_code == 302 and "/guest/bookings/" in response.location
            return int(response.location.rsplit("/", 1)[-1])
    pytest.fail(f"Room type {room_type_id} was not available")


def booking_state(app, booking_id):
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
            "checked_out_at": booking.checked_out_at,
        }


def test_staff_land_on_the_desk_and_guests_are_refused(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    staff = sign_in(app, staff_email)
    page = staff.get("/frontdesk")
    assert page.status_code == 200
    assert "Today's desk" in page.text and "Room board" in page.text
    guest = sign_in(app, guest_email)
    assert guest.get("/frontdesk").status_code == 403


def test_check_in_needs_payment_then_records_arrival_and_departure(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, room_id = make_room(app, "desk-king")
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, type_id, TODAY)
    staff = sign_in(app, staff_email)

    refused = post(staff, f"/frontdesk/bookings/{booking_id}/check-in", {},
                   form_path="/frontdesk")
    assert refused.status_code == 302
    assert booking_state(app, booking_id)["status"] == "confirmed"

    paid = post(staff, f"/frontdesk/bookings/{booking_id}/pay", {"pay-amount": "200.00"})
    assert paid.status_code == 302
    assert booking_state(app, booking_id)["status"] == "confirmed"

    checked_in = post(staff, f"/frontdesk/bookings/{booking_id}/check-in", {})
    assert checked_in.status_code == 302
    state = booking_state(app, booking_id)
    assert state["status"] == "checked_in" and state["checked_in_at"] is not None

    with app.app_context():
        assert db.session.query(PaymentRecord).filter_by(booking_id=booking_id).count() == 1
        assert db.session.query(AuditLog).filter_by(action="check_in").count() >= 1

    departed = post(staff, f"/frontdesk/bookings/{booking_id}/check-out", {})
    assert departed.status_code == 302
    state = booking_state(app, booking_id)
    assert state["status"] == "checked_out" and state["checked_out_at"] is not None


def test_partial_payment_is_refused_beyond_the_balance(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, _ = make_room(app, "desk-balance")
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, type_id, TODAY)
    staff = sign_in(app, staff_email)

    post(staff, f"/frontdesk/bookings/{booking_id}/pay", {"pay-amount": "500.00"})
    with app.app_context():
        assert db.session.query(PaymentRecord).filter_by(booking_id=booking_id).count() == 0


def test_block_conflict_then_reassign_keeps_the_price(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, room_id = make_room(app, "desk-block")
    with app.app_context():
        extra = Room(number="desk-block-2", room_type_id=type_id, operational_from=date(2020, 1, 1))
        db.session.add(extra)
        db.session.commit()
        spare_room = extra.id

    guest = sign_in(app, guest_email)
    arrival = TODAY + timedelta(days=2)
    booking_id = reserve(guest, type_id, arrival, nights=2)
    staff = sign_in(app, staff_email)

    blocked = post(staff, "/frontdesk/blocks", {
        "block-room_id": room_id, "block-start_date": str(arrival),
        "block-end_date": str(arrival + timedelta(days=1)), "block-reason": "Leaking pipe",
    })
    assert blocked.status_code == 302
    page = staff.get("/frontdesk")
    assert "Maintenance conflicts" in page.text
    assert "BK-" in page.text

    before = booking_state(app, booking_id)
    moved = post(staff, f"/frontdesk/bookings/{booking_id}/reassign", {"re-reason": "Pipe repair"})
    assert moved.status_code == 302
    after = booking_state(app, booking_id)
    assert after["room"] == spare_room and after["room"] != before["room"]
    assert after["nights"] == before["nights"] == 2
    assert after["total"] == before["total"]
    assert after["check_in"] == before["check_in"]


def test_reassign_without_a_free_room_changes_nothing(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, room_id = make_room(app, "desk-solid")
    guest = sign_in(app, guest_email)
    arrival = TODAY + timedelta(days=3)
    booking_id = reserve(guest, type_id, arrival, nights=1)
    staff = sign_in(app, staff_email)

    before = booking_state(app, booking_id)
    refused = post(staff, f"/frontdesk/bookings/{booking_id}/reassign", {"re-reason": "Try"})
    assert refused.status_code == 302
    after = booking_state(app, booking_id)
    assert after == before


def test_walk_in_books_a_room_and_reuses_allocation(app):
    _, staff_email = new_user(app, STAFF)
    type_id, room_id = make_room(app, "desk-walkin", price="300.00")
    staff = sign_in(app, staff_email)

    response = post(staff, "/frontdesk/walk-in", {
        "walk-name": "Walk In", "walk-phone": "13900000000", "walk-room_type_id": type_id,
        "walk-check_in": str(TODAY), "walk-check_out": str(TODAY + timedelta(days=1)),
        "walk-guests": 2, "walk-note": "Late arrival",
    })
    assert response.status_code == 302
    with app.app_context():
        booking = db.session.scalar(
            select(Booking).where(Booking.customer.has(Customer.name == "Walk In"))
        )
        assert booking is not None and booking.status == "confirmed"
        assert booking.total == Decimal("300.00")
        assert db.session.query(RoomNight).filter_by(booking_id=booking.id).count() == 1


def test_request_approval_is_atomic_and_rejection_keeps_the_booking(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, room_id = make_room(app, "desk-request")
    guest = sign_in(app, guest_email)
    arrival = TODAY + timedelta(days=1)
    booking_id = reserve(guest, type_id, arrival, nights=2)

    # A second booking takes the only room for the requested dates.
    guest_two_id, guest_two_email = new_user(app, GUEST)
    other = sign_in(app, guest_two_email)
    reserve(other, type_id, TODAY + timedelta(days=4), nights=2)

    with app.app_context():
        customer_id = db.session.scalar(select(Booking.customer_id).where(Booking.id == booking_id))
        request_row = BookingRequest(
            booking_id=booking_id, kind="dates",
            target={"check_in": str(TODAY + timedelta(days=4)),
                    "check_out": str(TODAY + timedelta(days=6))},
            reason="Move the stay", submitted_by=guest_two_id, status="pending",
        )
        db.session.add(request_row)
        db.session.commit()
        request_id = request_row.id

    staff = sign_in(app, staff_email)
    before = booking_state(app, booking_id)
    post(staff, f"/frontdesk/requests/{request_id}/approve", {})
    after = booking_state(app, booking_id)
    assert after["check_in"] == before["check_in"]
    assert after["nights"] == before["nights"]
    with app.app_context():
        assert db.session.get(BookingRequest, request_id).status == "pending"

    rejected = post(staff, f"/frontdesk/requests/{request_id}/reject", {"req-comment": "Sold out"})
    assert rejected.status_code == 302
    with app.app_context():
        row = db.session.get(BookingRequest, request_id)
        assert row.status == "rejected" and row.comment == "Sold out"
    assert booking_state(app, booking_id) == before


def test_cancellation_after_the_deadline_waits_for_the_manager(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, _ = make_room(app, "desk-cancel")
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, type_id, TODAY + timedelta(days=5))

    with app.app_context():
        row = BookingRequest(booking_id=booking_id, kind="cancel", target={},
                             reason="Plans changed", submitted_by=1, status="pending")
        # Submitted after the deadline: 18:00 the night before arrival.
        from datetime import datetime
        row.submitted_at = datetime(2030, 6, 23, 9, 0)
        db.session.add(row)
        db.session.commit()
        request_id = row.id

    staff = sign_in(app, staff_email)
    post(staff, f"/frontdesk/requests/{request_id}/approve", {})
    with app.app_context():
        assert db.session.get(BookingRequest, request_id).status == "pending"
        assert db.session.get(Booking, booking_id).status == "confirmed"


def test_guest_cannot_use_any_desk_action(app):
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    page = guest.get("/frontdesk")
    assert page.status_code == 403
    assert guest.post("/frontdesk/blocks", data={}).status_code in (400, 403)


def test_room_board_marks_arrived_guests_in_house(app):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, room_id = make_room(app, "desk-board")
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, type_id, TODAY)
    staff = sign_in(app, staff_email)
    post(staff, f"/frontdesk/bookings/{booking_id}/pay", {"pay-amount": ""})
    post(staff, f"/frontdesk/bookings/{booking_id}/check-in", {})

    with app.app_context():
        now = datetime(2030, 6, 18, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        board = desk_service.room_board(db.session, now.date(), 2)
        row = next(item for item in board if item["room"].id == room_id)
        assert row["cells"][0]["state"] == "in_house"


def test_room_board_marks_future_nights_of_unarrived_bookings_reserved(app):
    _, guest_email = new_user(app, GUEST)
    type_id, room_id = make_room(app, "desk-reserved")
    guest = sign_in(app, guest_email)
    reserve(guest, type_id, TODAY, nights=2)

    with app.app_context():
        board = desk_service.room_board(db.session, TODAY, 3)
        row = next(item for item in board if item["room"].id == room_id)
        assert row["cells"][0]["state"] == "arriving"
        assert row["cells"][1]["state"] == "reserved"

def test_reassign_moves_to_a_different_room(app):
    from datetime import date as date_type

    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, first_room = make_room(app, "desk-move")
    with app.app_context():
        second = Room(number="desk-move-2", room_type_id=type_id,
                      operational_from=date_type(2020, 1, 1))
        db.session.add(second)
        db.session.commit()
        second_room = second.id

    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, type_id, TODAY)
    staff = sign_in(app, staff_email)

    before = booking_state(app, booking_id)
    assert before["room"] == first_room

    post(staff, f"/frontdesk/bookings/{booking_id}/reassign", {"re-reason": "Guest asked"})
    after = booking_state(app, booking_id)
    assert after["room"] == second_room, "reassignment must not pick the same room again"
    assert after["nights"] == before["nights"] and after["total"] == before["total"]

def test_block_can_be_cancelled_and_the_room_returns(app):
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    type_id, room_id = make_room(app, "desk-release")
    guest = sign_in(app, guest_email)
    arrival = TODAY + timedelta(days=1)
    reserve(guest, type_id, arrival, nights=2)
    staff = sign_in(app, staff_email)

    post(staff, "/frontdesk/blocks", {
        "block-room_id": room_id, "block-start_date": str(arrival),
        "block-end_date": str(arrival + timedelta(days=1)), "block-reason": "Repaint",
    })
    with app.app_context():
        block = db.session.scalar(select(RoomBlock).where(RoomBlock.room_id == room_id))
        assert block is not None and block.effective
        block_id = block.id
        board = desk_service.room_board(db.session, arrival, 1)
        assert next(r for r in board if r["room"].id == room_id)["cells"][0]["state"] == "blocked"

    released = post(staff, f"/frontdesk/blocks/{block_id}/cancel", {})
    assert released.status_code == 302
    with app.app_context():
        block = db.session.get(RoomBlock, block_id)
        assert block.effective is False
        board = desk_service.room_board(db.session, arrival, 1)
        assert next(r for r in board if r["room"].id == room_id)["cells"][0]["state"] == "arriving"
        assert db.session.query(AuditLog).filter_by(action="cancel_block").count() == 1