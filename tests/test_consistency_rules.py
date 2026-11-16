"""A room that is still dirty cannot be sold for tonight.

This is the guest-side half of housekeeping: the desk marks a room dirty, the guest
search must stop offering it for the same day.
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
