from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Barrier, local
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app import allocation, models
from app.catalog import Stay
from app.extensions import db
from app.models import AuditLog, BlacklistEntry, Booking, BookingPriceLine, Customer, RoomNight, RoomType

from .helpers import make_guest


STAY = Stay(date(2026, 11, 12), date(2026, 11, 14), 1)


def family_type(app):
    with app.app_context():
        return db.session.scalar(select(RoomType.id).where(RoomType.code == "demo-family"))


def counts(app):
    with app.app_context():
        return [
            db.session.scalar(select(func.count()).select_from(model))
            for model in (Booking, RoomNight, BookingPriceLine, AuditLog)
        ]


def submit_pair(app, monkeypatch, actors, room_type_id, keys):
    barrier = Barrier(2)
    thread = local()
    lookup = allocation.existing_booking

    def simultaneous_lookup(session, actor_id, key):
        found = lookup(session, actor_id, key)
        if not getattr(thread, "looked_up", False):
            thread.looked_up = True
            assert found is None
            barrier.wait(timeout=10)
        return found

    def submit(actor, key):
        with app.app_context():
            try:
                return allocation.create_booking(actor, room_type_id, STAY, key)
            except allocation.BookingRejected as exc:
                return exc.code

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(allocation, "existing_booking", simultaneous_lookup)
        requests = [pool.submit(submit, actor, key) for actor, key in zip(actors, keys)]
        return [future.result(timeout=20) for future in requests]


def test_two_guests_contend_for_last_room(demo, monkeypatch):
    actor = make_guest(demo, "first@example.test")
    other = make_guest(demo, "second@example.test")
    results = submit_pair(demo, monkeypatch, [actor, other], family_type(demo), [str(uuid4()), str(uuid4())])
    assert sum(isinstance(item, allocation.BookingResult) for item in results) == 1
    assert results.count("SOLD_OUT") == 1
    assert counts(demo) == [1, 2, 2, 1]


def test_failed_fifth_night_rolls_back_flushed_rows(demo, monkeypatch):
    actor = make_guest(demo, "rollback@example.test")
    room_type = family_type(demo)
    add_night = allocation.add_night
    five_nights = Stay(STAY.check_in, date(2026, 11, 17), 1)
    fifth_night = date(2026, 11, 16)

    def fail_fifth_night(session, booking, day, rate):
        if day == fifth_night:
            raise RuntimeError("Simulated fifth-night write failure")
        add_night(session, booking, day, rate)
        session.flush()

    monkeypatch.setattr(allocation, "add_night", fail_fifth_night)
    with demo.app_context(), pytest.raises(RuntimeError, match="fifth-night"):
        allocation.create_booking(actor, room_type, five_nights, str(uuid4()))
    assert counts(demo) == [0, 0, 0, 0]


def test_concurrent_replay_returns_same_booking(demo, monkeypatch):
    actor = make_guest(demo, "replay@example.test")
    room_type = family_type(demo)
    key = str(uuid4())
    first, repeated = submit_pair(demo, monkeypatch, [actor, actor], room_type, [key, key])
    assert isinstance(first, allocation.BookingResult)
    assert first == repeated
    assert counts(demo) == [1, 2, 2, 1]
    with demo.app_context(), pytest.raises(allocation.BookingRejected) as error:
        allocation.create_booking(actor, room_type, Stay(STAY.check_in, STAY.check_out, 2), key)
    assert error.value.code == "IDEMPOTENCY_MISMATCH"


def test_blacklist_is_checked_and_booking_updates_search(demo, client, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 11, 7, 0, 0, 900000, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(models, "datetime", FixedDatetime)
    actor = make_guest(demo, "blocked@example.test")
    room_type = family_type(demo)
    with demo.app_context():
        customer = db.session.scalar(select(Customer).where(Customer.user_id == actor))
        entry = BlacklistEntry(customer_id=customer.id, actor_user_id=actor, reason="Test restriction")
        db.session.add(entry)
        db.session.commit()
        with pytest.raises(allocation.BookingRejected) as error:
            allocation.create_booking(actor, room_type, STAY, str(uuid4()))
        assert error.value.code == "FORBIDDEN"
        db.session.delete(entry)
        db.session.commit()
        allocation.create_booking(actor, room_type, STAY, str(uuid4()))
    taken = client.get("/api/availability?check_in=2026-11-12&check_out=2026-11-14&guests=4")
    adjacent = client.get("/api/availability?check_in=2026-11-14&check_out=2026-11-15&guests=4")
    assert taken.json["room_types"] == []
    assert [item["name"] for item in adjacent.json["room_types"]] == ["Family Suite"]
