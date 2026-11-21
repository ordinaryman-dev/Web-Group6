"""Real-MySQL checks for workload rotation and transactional allocation retries."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from threading import Barrier, Lock
from uuid import uuid4

import pymysql
import pytest
from flask_login import login_user
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import allocation, booking, desk
from app.extensions import db
from app.frontdesk import _run
from app.models import (
    AuditLog, Booking, BookingPriceLine, Customer, GUEST, Room, RoomNight,
    RoomBlock, RoomType, STAFF, User,
)


TODAY = date(2030, 6, 18)


@pytest.fixture
def inventory(app):
    with app.app_context():
        label = f"rot-{uuid4().hex[:12]}"
        room_type = RoomType(code=label, name=label, description="Rotation test",
                             capacity=2, base_price=Decimal("200"))
        guest = User(email=f"{label}@example.test", password_hash="unused", role=GUEST)
        staff = User(email=f"staff-{label}@example.test", password_hash="unused", role=STAFF)
        customer = Customer(user=guest, name=label, phone=label)
        db.session.add_all([room_type, guest, staff, customer])
        db.session.flush()
        rooms = [Room(number=f"{label}-{i}", room_type_id=room_type.id,
                      operational_from=date(2020, 1, 1)) for i in range(3)]
        db.session.add_all(rooms)
        db.session.commit()
        return dict(type=room_type.id, rooms=[room.id for room in rooms],
                    guest=guest.id, staff=staff.id, customer=customer.id, label=label)


def seed_booking(session, data, room_id, start, end, *, status="confirmed", arrived=None, departed=None):
    row = Booking(customer_id=data["customer"], actor_user_id=data["guest"],
                  room_type_id=data["type"], room_id=room_id, check_in=start, check_out=end,
                  guests=1, total=Decimal("200"), idempotency_key=str(uuid4()),
                  request_digest="fixture", status=status,
                  checked_in_at=arrived, checked_out_at=departed)
    session.add(row)
    session.flush()
    for day in range((end - start).days):
        session.add(RoomNight(room_id=room_id, booking_id=row.id,
                              stay_date=start + timedelta(days=day)))
    return row


def reserve(data, offset=2, nights=1, key=None):
    result = booking.create_booking(data["guest"], data["type"],
                                    booking.Stay(TODAY + timedelta(days=offset),
                                                 TODAY + timedelta(days=offset + nights), 1),
                                    key or str(uuid4()), expected_arrival=time(18))
    # The request commits through its own Session; verify with a new read snapshot.
    db.session.rollback()
    return result


def test_load_counts_actual_recent_nights_and_all_committed_future_nights(app, inventory):
    data = inventory
    first, second, third = data["rooms"]
    with app.app_context():
        # Only days -30 through -5 count: operational history older than 30 days
        # and nights after the actual early departure do not contribute.
        seed_booking(db.session, data, first, TODAY - timedelta(days=35), TODAY - timedelta(days=2),
                     status="checked_out", arrived=datetime(2030, 5, 17, 15, 30),
                     departed=datetime(2030, 6, 14, 4))
        seed_booking(db.session, data, first, TODAY + timedelta(days=4), TODAY + timedelta(days=25))
        for offset, status in ((-12, "cancelled"), (-8, "confirmed"), (40, "no_show"), (44, "checked_out")):
            seed_booking(db.session, data, second, TODAY + timedelta(days=offset),
                         TODAY + timedelta(days=offset + 2), status=status)
        # 16:30 UTC is 00:30 next day in Beijing: count only the last completed night.
        seed_booking(db.session, data, third, TODAY - timedelta(days=2), TODAY + timedelta(days=1),
                     status="checked_out", arrived=datetime(2030, 6, 16, 16, 30),
                     departed=datetime(2030, 6, 18, 16))
        db.session.commit()
        rows = allocation.ranked_candidates(db.session, data["type"],
                                            booking.Stay(TODAY + timedelta(days=2), TODAY + timedelta(days=3), 1), TODAY)
        counts = {row.room_id: (int(row.past_nights), int(row.future_nights)) for row in rows}
        assert counts == {first: (26, 21), second: (0, 0), third: (1, 0)}


def test_lowest_night_load_precedes_oldest_assignment_and_id(app, inventory):
    data = inventory
    first, second, third = data["rooms"]
    with app.app_context():
        for room_id, nights, stamp in ((first, 6, datetime(2019, 1, 1)),
                                      (second, 2, datetime(2019, 3, 1)),
                                      (third, 2, datetime(2019, 2, 1))):
            seed_booking(db.session, data, room_id, TODAY + timedelta(days=7),
                         TODAY + timedelta(days=7 + nights))
            db.session.get(Room, room_id).last_allocated_at = stamp
        db.session.commit()
        result = reserve(data)
        assert db.session.get(Booking, result.id).room_id == third
        assert db.session.get(Room, third).last_allocated_at > datetime(2019, 3, 1)


def test_tied_rooms_rotate_and_idempotent_replay_does_not_rotate(app, inventory):
    data = inventory
    with app.app_context():
        key = str(uuid4())
        first = reserve(data, key=key)
        room_id = db.session.get(Booking, first.id).room_id
        timestamp = db.session.get(Room, room_id).last_allocated_at
        assert reserve(data, key=key).id == first.id
        db.session.expire_all()
        assert db.session.get(Room, room_id).last_allocated_at == timestamp
        chosen = [room_id]
        # Non-overlapping stays still spread across rooms instead of reusing room 1.
        for offset in (3, 4):
            result = reserve(data, offset=offset)
            chosen.append(db.session.get(Booking, result.id).room_id)
        assert chosen == data["rooms"]
        # Clear the future load but retain assignment history, as cancellation does.
        db.session.execute(db.delete(RoomNight).where(RoomNight.room_id.in_(chosen)))
        db.session.commit()
        fourth = reserve(data, offset=5)
        assert db.session.get(Booking, fourth.id).room_id == chosen[0]


def test_online_and_staff_automatic_and_explicit_assignments_share_rotation(app, inventory):
    data = inventory
    with app.app_context():
        first = reserve(data, offset=1)
        assert db.session.get(Booking, first.id).room_id == data["rooms"][0]
        with Session(db.engine.execution_options(isolation_level="READ COMMITTED")) as session, session.begin():
            walkin = desk.create_walkin(session, data["staff"], data["type"],
                                       booking.Stay(TODAY, TODAY + timedelta(days=1), 1),
                                       customer_id=data["customer"])
            assert walkin.room_id == data["rooms"][1]
        third = reserve(data, offset=2)
        assert db.session.get(Booking, third.id).room_id == data["rooms"][2]
        with Session(db.engine.execution_options(isolation_level="READ COMMITTED")) as session, session.begin():
            manual = desk.create_walkin(session, data["staff"], data["type"],
                                       booking.Stay(TODAY, TODAY + timedelta(days=1), 1),
                                       customer_id=data["customer"], room_id=data["rooms"][0])
            assert manual.room_id == data["rooms"][0]


def test_staff_http_walkin_accepts_automatic_room_selection(app, inventory):
    data = inventory
    with app.app_context():
        seed_booking(db.session, data, data["rooms"][0], TODAY + timedelta(days=7), TODAY + timedelta(days=12))
        db.session.commit()
    client = app.test_client()
    with client.session_transaction() as cookie:
        cookie["_user_id"] = str(data["staff"])
        cookie["_fresh"] = True
    page = client.get("/frontdesk")
    csrf = re.search(r'name="[^"]*csrf_token"[^>]*value="([^"]+)"', page.text).group(1)
    response = client.post("/frontdesk/walk-in", data={
        "walk-csrf_token": csrf, "walk-room_id": "", "walk-room_type_id": str(data["type"]),
        "walk-customer_id": str(data["customer"]), "walk-check_in": str(TODAY),
        "walk-check_out": str(TODAY + timedelta(days=1)), "walk-guests": "1",
    })
    assert response.status_code == 302 and "booking=" in response.location
    with app.app_context():
        row = db.session.scalar(select(Booking).where(Booking.actor_user_id == data["staff"]))
        assert row.room_id == data["rooms"][1] and row.status == "checked_in"


def test_failed_assignment_rolls_back_room_rotation_and_booking_records(app, inventory, monkeypatch):
    data = inventory

    def fail_audit(*args, **kwargs):
        raise RuntimeError("write failed after room assignment")

    with app.app_context(), monkeypatch.context() as patch:
        patch.setattr(booking, "record_audit", fail_audit)
        with pytest.raises(RuntimeError, match="write failed"):
            reserve(data)
        assert db.session.scalar(select(func.count()).select_from(Booking).where(Booking.room_type_id == data["type"])) == 0
        assert db.session.scalar(select(func.count()).select_from(RoomNight).where(RoomNight.room_id.in_(data["rooms"]))) == 0
        assert db.session.scalar(select(func.count()).select_from(BookingPriceLine).join(Booking).where(Booking.room_type_id == data["type"])) == 0
        assert all(db.session.get(Room, room_id).last_allocated_at is None for room_id in data["rooms"])


@pytest.mark.parametrize("error_code", [1205, 1213])
def test_staff_retry_uses_new_session_and_rolls_back_new_customer(app, inventory, monkeypatch, error_code):
    data = inventory
    sessions = []
    original_audit = desk.record_audit

    def retry_once(session, *args, **kwargs):
        sessions.append(session)
        session.flush()  # Customer, order, price, payment and timestamp really reached MySQL.
        if len(sessions) == 1:
            raise OperationalError("test", {}, pymysql.err.OperationalError(error_code, "retry test"))
        return original_audit(session, *args, **kwargs)

    with app.test_request_context("/frontdesk"), monkeypatch.context() as patch:
        login_user(db.session.get(User, data["staff"]))
        patch.setattr(desk, "record_audit", retry_once)
        assert _run(lambda session: desk.create_walkin(
            session, data["staff"], data["type"], booking.Stay(TODAY, TODAY + timedelta(days=1), 1),
            name=data["label"], phone=f"new-{data['label']}"), "Created")
        db.session.rollback()
        assert len(sessions) == 2 and sessions[0] is not sessions[1]
        assert db.session.query(Customer).filter_by(phone=f"new-{data['label']}").count() == 1
        assert db.session.query(Booking).filter_by(room_type_id=data["type"]).count() == 1
        assert db.session.query(RoomNight).filter(RoomNight.room_id.in_(data["rooms"])).count() == 1
        assert db.session.query(AuditLog).join(Booking, Booking.id == AuditLog.object_id).filter(
            AuditLog.object_type == "booking", AuditLog.action == "create_booking",
            Booking.room_type_id == data["type"]).count() == 1


def test_stale_candidate_is_retried_before_using_another_room(app, inventory, monkeypatch):
    data = inventory
    original_rank = allocation.ranked_candidates
    gate = Barrier(2)
    calls = {}

    def synchronized_rank(session, *args, **kwargs):
        result = original_rank(session, *args, **kwargs)
        calls[session] = calls.get(session, 0) + 1
        if calls[session] == 1 and len([count for count in calls.values() if count >= 1]) <= 2:
            gate.wait(timeout=10)  # Both saw the same preferred room before either locked it.
        return result

    def book(_):
        with app.app_context():
            return reserve(data).id

    with monkeypatch.context() as patch:
        patch.setattr(allocation, "ranked_candidates", synchronized_rank)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(book, range(2)))
    with app.app_context():
        rooms = db.session.scalars(select(Booking.room_id).where(Booking.id.in_(results))).all()
        assert len(set(rooms)) == 2
        assert len(calls) == 3  # Two initial Sessions and one fresh retry.


def test_real_mysql_deadlock_rolls_back_then_retries_successfully(app, inventory, monkeypatch):
    data = inventory
    gate = Barrier(2)
    attempts = {}
    attempt_lock = Lock()
    errors = []
    original_allocate = booking.allocate_once
    with app.app_context():
        engine = db.engine

    def capture_error(context):
        if context.original_exception.args[0] == 1213:
            errors.append(1213)

    def force_deadlock(session, *args, **kwargs):
        key = args[3]
        with attempt_lock:
            attempts.setdefault(key, []).append(session)
            first_attempt = len(attempts[key]) == 1
            reverse = len(attempts) == 2
        if first_attempt:
            first, second = data["rooms"][:2][::(-1 if reverse else 1)]
            session.get(Room, first, with_for_update=True)
            gate.wait(timeout=10)
            session.get(Room, second, with_for_update=True)
        return original_allocate(session, *args, **kwargs)

    def book(_):
        with app.app_context():
            return reserve(data).id

    event.listen(engine, "handle_error", capture_error)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(booking, "allocate_once", force_deadlock)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(book, range(2)))
    finally:
        event.remove(engine, "handle_error", capture_error)
    assert errors == [1213]
    # One request wins without a deadlock. The loser retries at least once; its
    # retry can legitimately lose the candidate race again and need one more
    # allocation retry, so accept 2 or 3 attempts as long as each uses a fresh
    # Session and the two bookings still end up in different rooms.
    counts = sorted(len(sessions) for sessions in attempts.values())
    assert counts[0] == 1 and 2 <= counts[1] <= 3
    for sessions in attempts.values():
        assert len({id(session) for session in sessions}) == len(sessions)
    with app.app_context():
        rooms = db.session.scalars(select(Booking.room_id).where(Booking.id.in_(results))).all()
        assert len(set(rooms)) == 2


def test_reassignment_uses_shared_ranking_and_locks_rooms_in_id_order(app, inventory):
    data = inventory
    first, second, third = data["rooms"]
    with app.app_context():
        booking_id = reserve(data).id
        seed_booking(db.session, data, second, TODAY + timedelta(days=7), TODAY + timedelta(days=10))
        db.session.commit()
        with Session(db.engine.execution_options(isolation_level="READ COMMITTED")) as session, session.begin():
            moved = desk.reassign(session, booking_id, data["staff"], "Maintenance", today=TODAY)
            assert moved.room_id == third
        assert db.session.query(RoomNight).filter_by(room_id=first).count() == 0
        db.session.expire_all()
        assert db.session.get(Room, third).last_allocated_at is not None
        db.session.rollback()
        locked = []

        def capture_locks(conn, cursor, statement, parameters, context, executemany):
            if "FROM rooms" in statement and "FOR UPDATE" in statement:
                locked.append(parameters["pk_1"])

        event.listen(db.engine, "before_cursor_execute", capture_locks)
        try:
            with Session(db.engine.execution_options(isolation_level="READ COMMITTED")) as session, session.begin():
                moved = desk.reassign(session, booking_id, data["staff"], "Move back", today=TODAY)
                assert moved.room_id == first
        finally:
            event.remove(db.engine, "before_cursor_execute", capture_locks)
        assert locked == [first, third]  # Descending move still acquires ascending locks.


def test_cancelled_maintenance_does_not_exclude_a_room(app, inventory):
    data = inventory
    with app.app_context():
        db.session.add(RoomBlock(room_id=data["rooms"][0], start_date=TODAY,
                                 end_date=TODAY + timedelta(days=7), reason="Released", effective=False))
        db.session.commit()
        result = reserve(data)
        assert db.session.get(Booking, result.id).room_id == data["rooms"][0]


def test_contention_exhaustion_returns_busy_without_partial_changes(app, inventory, monkeypatch):
    data = inventory
    sessions = []

    def always_stale(session, *args, **kwargs):
        sessions.append(session)
        allocation.record_assignment(session.get(Room, data["rooms"][0], with_for_update=True))
        session.flush()
        raise allocation.AllocationRetry()

    with app.app_context(), monkeypatch.context() as patch:
        patch.setattr(allocation, "select_room", always_stale)
        with pytest.raises(booking.BookingRejected) as error:
            reserve(data)
        assert error.value.code == "BUSY_RETRY"
        assert len(sessions) == 3 and len({id(session) for session in sessions}) == 3
        assert db.session.query(Booking).filter_by(room_type_id=data["type"]).count() == 0
        assert db.session.get(Room, data["rooms"][0]).last_allocated_at is None
