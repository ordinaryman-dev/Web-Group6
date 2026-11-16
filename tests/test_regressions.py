"""Regressions for front-desk chores that had no automated cover.

* check-out turns the room dirty and it cannot be sold until housekeeping is done;
* the window filter really separates rooms with and without a window;
* an overdue in-house guest keeps the room off the market.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import desk as desk_service
from app.booking import Stay, free_room_conditions
from app.extensions import db
from app.models import Room, STAFF
from test_frontdesk import new_user, sign_in
from test_frontdesk_rules import make_booking, make_type_and_rooms, staff_id

TODAY = date(2030, 6, 18)
NOON = datetime(2030, 6, 18, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def free_rooms(app, stay, room_ids):
    with app.app_context():
        return {
            room_id
            for room_id in room_ids
            if db.session.scalar(
                select(Room.id).where(Room.id == room_id, *free_room_conditions(stay))
            )
        }


def card_classes(html, number):
    match = re.search(
        r'<a class="room-card([^"]*)"[^>]*title="Room ' + re.escape(str(number)) + r"\b",
        html,
    )
    assert match, f"room {number} is not on the board"
    return match.group(1)


def test_check_out_makes_the_room_dirty_until_it_is_cleaned(app):
    type_id, rooms = make_type_and_rooms(app, "reg-clean", count=1)
    # Last night's guest leaves this morning, so tonight is the night on sale.
    booking_id = make_booking(app, type_id, rooms, check_in=TODAY - timedelta(days=1),
                              nights=1, status="checked_in")
    actor = staff_id(app)
    stay = Stay(TODAY, TODAY + timedelta(days=1), 2)

    assert free_rooms(app, stay, rooms) == set(rooms)

    with app.app_context():
        desk_service.check_out(db.session, booking_id, actor, NOON)
        db.session.commit()
        assert db.session.get(Room, rooms[0]).housekeeping == "dirty"

    assert free_rooms(app, stay, rooms) == set()

    with app.app_context():
        desk_service.mark_room_clean(db.session, rooms[0], actor)
        db.session.commit()
        assert db.session.get(Room, rooms[0]).housekeeping == "clean"

    assert free_rooms(app, stay, rooms) == set(rooms)


def test_the_window_filter_separates_rooms_with_and_without_windows(app):
    type_id, rooms = make_type_and_rooms(app, "reg-win", count=2)
    with app.app_context():
        day = db.session.get(Room, rooms[0])
        day.has_window = True
        dark = db.session.get(Room, rooms[1])
        dark.has_window = False
        db.session.commit()
        window_number, dark_number = day.number, dark.number

    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    page = staff.get("/frontdesk", query_string={
        "view": "grid", "window": "without", "floor": "REG",
    })
    assert page.status_code == 200

    assert "is-dim" in card_classes(page.text, window_number)
    assert "is-dim" not in card_classes(page.text, dark_number)


def test_an_overdue_guest_keeps_the_room_off_the_market(app):
    type_id, rooms = make_type_and_rooms(app, "reg-overdue", count=1)
    booking_id = make_booking(app, type_id, rooms, check_in=TODAY - timedelta(days=3),
                              nights=2, status="checked_in")
    actor = staff_id(app)
    stay = Stay(TODAY, TODAY + timedelta(days=1), 2)

    assert free_rooms(app, stay, rooms) == set()

    with app.app_context():
        desk_service.check_out(db.session, booking_id, actor, NOON)
        desk_service.mark_room_clean(db.session, rooms[0], actor)
        db.session.commit()

    assert free_rooms(app, stay, rooms) == set(rooms)


def test_seed_desk_reset_rebuilds_the_demo_bookings(app):
    from app.models import Booking

    runner = app.test_cli_runner()
    first = runner.invoke(args=["seed-desk"])
    assert first.exit_code == 0, first.output

    with app.app_context():
        before = db.session.query(Booking).filter_by(request_digest="seed").count()
    assert before > 0

    again = runner.invoke(args=["seed-desk", "--reset"])
    assert again.exit_code == 0, again.output
    assert "Removed" in again.output

    with app.app_context():
        after = db.session.query(Booking).filter_by(request_digest="seed").count()
        today = {booking.check_in for booking in
                 db.session.query(Booking).filter_by(request_digest="seed")}
    assert after == before
    assert TODAY in today
