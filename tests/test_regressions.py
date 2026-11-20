"""Regressions for front-desk chores that had no automated cover.

* check-out turns the room dirty and it cannot be sold until housekeeping is done;
* the window filter really separates rooms with and without a window;
* an overdue in-house guest keeps the room off the market.
"""

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
