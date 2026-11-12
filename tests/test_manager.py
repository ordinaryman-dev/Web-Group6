import csv
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import unescape
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from app import booking as allocations
from app.extensions import db
from app.models import (
    AuditLog, BlacklistEntry, Booking, BookingPriceLine, BookingRequest, Customer, GUEST, MANAGER,
    PaymentRecord, PriceRule, Room, RoomBlock, RoomNight, RoomType, STAFF, User,
)


TODAY = date(2030, 6, 18)
PASSWORD = "A password for integration tests"


def token(page):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.text).group(1)


def post(client, path, data, *, form_path=None):
    return client.post(path, data={**data, "csrf_token": token(client.get(form_path or path))})


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
    response = post(client, "/login", {"email": email, "password": password})
    assert response.status_code == 302
    return client, urlparse(response.location).path


def create_room(app, label, *, from_date=TODAY):
    with app.app_context():
        room_type = RoomType(code=label, name=label, description="Test room", capacity=2, base_price=Decimal("200"))
        db.session.add(room_type)
        db.session.flush()
        room = Room(number=label, room_type_id=room_type.id, operational_from=from_date)
        db.session.add(room)
        db.session.commit()
        return room_type.id, room.id


def booking_fields(client, room_type_id, check_in, nights=2):
    # The guest flow books from the confirmation page, which carries the locked quote.
    query = {"check_in": str(check_in), "check_out": str(check_in + timedelta(days=nights)), "guests": 2}
    page = client.get(f"/guest/book/{room_type_id}", query_string=query)
    assert page.status_code == 200, page.status_code
    form = re.search(r'<form method="post" action="/guest/book".*?</form>', page.text, re.S)
    if form is None:
        pytest.fail(f"Room type {room_type_id} was not available")
    fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form.group(0)))
    fields.pop("submit", None)
    fields.setdefault("expected_arrival", "14:00")
    return {**query, **fields, "note": "Please prepare extra towels"}



def reserve(client, room_type_id, day, nights=2):
    form = booking_fields(client, room_type_id, day, nights)
    response = client.post("/guest/book", data=form)
    assert response.status_code == 302 and "/guest/bookings/" in response.location
    return int(response.location.rsplit("/", 1)[-1]), form


def test_existing_rate_survives_manager_migration(app):
    with app.app_context():
        rule = db.session.scalar(select(PriceRule).join(RoomType).where(RoomType.code == "legacy-type"))
        assert rule.active and rule.amount == Decimal("205.00")
        assert rule.start_date == date(2028, 6, 1) and rule.end_date == date(2028, 7, 1)


def test_role_destinations_and_manager_permissions(app):
    manager_email = "bootstrap-manager@example.test"
    created = app.test_cli_runner().invoke(args=["create-manager", manager_email], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert created.exit_code == 0, created.output
    _, staff_email = new_user(app, STAFF)
    _, guest_email = new_user(app, GUEST)
    anonymous = app.test_client()
    assert urlparse(anonymous.get("/manage").location).path == "/login"
    manager, target = sign_in(app, manager_email)
    assert target == "/manage"
    for path in (
        "/manage", "/manage/room-types", "/manage/room-types/new", "/manage/rooms", "/manage/rooms/new",
        "/manage/prices", "/manage/prices/new", "/manage/accounts", "/manage/accounts/new",
        "/manage/blacklist", "/manage/bookings", "/manage/reports", "/manage/audit", "/manage/profile",
    ):
        assert manager.get(path).status_code == 200, path
    for email, workspace in ((staff_email, "/frontdesk"), (guest_email, "/guest/")):
        client, target = sign_in(app, email)
        assert target == workspace
        assert client.get("/manage").status_code == 403
        assert post(client, "/manage/accounts/new", {}, form_path=workspace).status_code == 403
    assert manager.post("/manage/accounts/new", data={"email": "no-csrf@example.test"}).status_code == 400


def test_catalogue_price_rule_precedence_and_reconfirmation(app):
    _, manager_email = new_user(app, MANAGER)
    _, guest_email = new_user(app, GUEST)
    manager, _ = sign_in(app, manager_email)
    guest, _ = sign_in(app, guest_email)
    type_fields = {
        "code": "manager-suite", "name": "Manager test suite", "description": "A test room type",
        "capacity": 8, "base_price": "200.00", "listed": "y", "bed_type": "suite",
    }
    assert post(manager, "/manage/room-types/new", type_fields).status_code == 302
    with app.app_context():
        type_id = db.session.scalar(select(RoomType.id).where(RoomType.code == "manager-suite"))
    room = {"number": "manager-101", "room_type_id": type_id, "operational_from": str(TODAY)}
    assert post(manager, "/manage/rooms/new", room).status_code == 302

    arrival = TODAY + timedelta(days=2)
    season = {"room_type_id": type_id, "kind": "season", "start_date": str(arrival),
              "end_date": str(arrival + timedelta(days=4)), "amount": "250.00"}
    override = {**season, "kind": "date", "end_date": str(arrival + timedelta(days=1)), "amount": "310.00"}
    assert post(manager, "/manage/prices/new", season).status_code == 302
    assert post(manager, "/manage/prices/new", {**season, "start_date": str(arrival + timedelta(days=1))}).status_code == 409
    assert post(manager, "/manage/prices/new", {**override, "amount": "bad"}).status_code == 400
    assert post(manager, "/manage/prices/new", override).status_code == 302
    with app.app_context():
        original_rule_id = db.session.scalar(select(PriceRule.id).where(
            PriceRule.room_type_id == type_id, PriceRule.kind == "date",
        ))
    stale = booking_fields(guest, type_id, arrival)
    assert post(manager, f"/manage/prices/{original_rule_id}", {**override, "amount": "330.00"}).status_code == 302
    rejected = guest.post("/guest/book", data=stale, follow_redirects=True)
    assert "The price changed" in rejected.text
    with app.app_context():
        assert not db.session.query(Booking).filter_by(room_type_id=type_id).count()
        assert not db.session.get(PriceRule, original_rule_id).active
        assert db.session.get(PriceRule, original_rule_id).amount == Decimal("310.00")
    booking_id, _ = reserve(guest, type_id, arrival)
    assert post(manager, f"/manage/room-types/{type_id}", {**type_fields, "listed": "", "base_price": "499.00"}).status_code == 302
    with app.app_context():
        booking = db.session.get(Booking, booking_id)
        assert booking.total == Decimal("580.00")
        assert [line.amount for line in booking.price_lines] == [Decimal("330.00"), Decimal("250.00")]
        assert not db.session.get(RoomType, type_id).listed
    assert "<h3>Manager test suite</h3>" not in guest.get("/guest/").text
    assert guest.get(f"/guest/bookings/{booking_id}").status_code == 200


def test_staff_disable_and_password_change_preserve_history(app):
    manager_id, manager_email = new_user(app, MANAGER)
    manager, _ = sign_in(app, manager_email)
    email = "console-staff@example.test"
    fields = {"email": email, "password": PASSWORD, "confirm": PASSWORD}
    assert post(manager, "/manage/accounts/new", fields).status_code == 302
    assert post(manager, "/manage/accounts/new", fields).status_code == 409
    staff, _ = sign_in(app, email)
    with app.app_context():
        staff_id = db.session.scalar(select(User.id).where(User.email == email))
    assert post(manager, f"/manage/accounts/{staff_id}/disable", {}, form_path="/manage/accounts").status_code == 302
    assert urlparse(staff.get("/frontdesk").location).path == "/login"
    assert post(app.test_client(), "/login", {"email": email, "password": PASSWORD}).status_code == 401
    change = {"current_password": PASSWORD, "new_password": "A new long password", "confirm": "A new long password"}
    assert post(manager, "/manage/profile", change).status_code == 302
    _, target = sign_in(app, manager_email, change["new_password"])
    assert target == "/manage"
    with app.app_context():
        assert not db.session.get(User, staff_id).active
        assert check_password_hash(db.session.get(User, manager_id).password_hash, change["new_password"])
        entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "disable_staff", AuditLog.object_id == staff_id))
        assert entry.actor_user_id == manager_id and entry.details["after"]["active"] is False
    audit = manager.get("/manage/audit", query_string={"actor": manager_email, "action": "disable_staff", "object_id": staff_id})
    assert audit.status_code == 200 and manager_email in audit.text


def test_blacklist_controls_booking_but_preserves_old_order(app):
    _, manager_email = new_user(app, MANAGER)
    guest_id, guest_email = new_user(app, GUEST)
    type_id, _ = create_room(app, "blacklist-room")
    manager, _ = sign_in(app, manager_email)
    guest, _ = sign_in(app, guest_email)
    existing, _ = reserve(guest, type_id, TODAY + timedelta(days=2))
    form = booking_fields(guest, type_id, TODAY + timedelta(days=6))
    with app.app_context():
        customer_id = db.session.scalar(select(Customer.id).where(Customer.user_id == guest_id))
    fields = {"customer_id": customer_id, "reason": "Hotel policy test"}
    assert post(manager, "/manage/blacklist", fields).status_code == 302
    assert post(manager, "/manage/blacklist", fields).status_code == 409
    assert guest.get(f"/guest/bookings/{existing}").status_code == 200
    assert "cannot make a new booking" in guest.post("/guest/book", data=form, follow_redirects=True).text
    assert post(manager, f"/manage/blacklist/{customer_id}/remove", {"reason": "Reviewed"}).status_code == 302
    assert "/guest/bookings/" in guest.post("/guest/book", data=form).location
    with app.app_context():
        assert db.session.get(Booking, existing).status == "confirmed"
        assert db.session.scalar(select(BlacklistEntry).where(BlacklistEntry.customer_id == customer_id)).removed_at


def test_concurrent_cancellation_refunds_net_once_and_releases_room(app):
    manager_id, manager_email = new_user(app, MANAGER)
    guest_id, guest_email = new_user(app, GUEST)
    type_id, room_id = create_room(app, "cancel-room")
    first, _ = sign_in(app, manager_email)
    second, _ = sign_in(app, manager_email)
    guest, _ = sign_in(app, guest_email)
    booking_id, _ = reserve(guest, type_id, TODAY)
    with app.app_context():
        for kind, amount, result in (("payment", "180", "success"), ("refund", "20", "success"), ("payment", "95", "failed")):
            db.session.add(PaymentRecord(
                booking_id=booking_id, kind=kind, amount=Decimal(amount), result=result,
                actor_user_id=manager_id, reference=str(uuid4()),
            ))
        db.session.add(BookingRequest(booking_id=booking_id, kind="cancel", submitted_by=guest_id))
        db.session.commit()

    detail = f"/manage/bookings/{booking_id}"
    gate = Barrier(2)
    clients = [(client, token(client.get(detail))) for client in (first, second)]

    def cancel(item):
        client, csrf = item
        gate.wait(timeout=10)
        return client.post(f"{detail}/cancel", data={"source": "exception", "reason": "Late emergency", "csrf_token": csrf})

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(response.status_code for response in pool.map(cancel, clients)) == [302, 409]
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "cancelled"
        assert db.session.query(RoomNight).filter_by(booking_id=booking_id).count() == 0
        assert db.session.query(BookingPriceLine).filter_by(booking_id=booking_id).count() == 2
        refunds = db.session.scalars(select(PaymentRecord.amount).where(
            PaymentRecord.booking_id == booking_id, PaymentRecord.kind == "refund",
        ).order_by(PaymentRecord.id)).all()
        assert refunds == [Decimal("20.00"), Decimal("160.00")]
        assert db.session.scalar(select(BookingRequest.status).where(BookingRequest.booking_id == booking_id)) == "closed"
        assert db.session.query(AuditLog).filter_by(action="cancel_booking", object_id=booking_id).count() == 1
    assert "160.00" in guest.get(f"/guest/bookings/{booking_id}").text
    replacement, _ = reserve(guest, type_id, TODAY)
    assert replacement != booking_id
    with app.app_context():
        assert db.session.get(Booking, replacement).room_id == room_id


def test_cancellation_failure_rolls_back_and_unpaid_has_no_refund(app, monkeypatch):
    _, manager_email = new_user(app, MANAGER)
    _, guest_email = new_user(app, GUEST)
    type_id, _ = create_room(app, "rollback-room")
    manager, _ = sign_in(app, manager_email)
    guest, _ = sign_in(app, guest_email)
    booking_id, _ = reserve(guest, type_id, TODAY + timedelta(days=4))
    detail = f"/manage/bookings/{booking_id}"
    reason = {"source": "hotel", "reason": "Hotel incident"}

    def fail_audit(*args, **kwargs):
        raise RuntimeError("Simulated write failure")

    assert post(manager, f"{detail}/cancel", {**reason, "source": "exception"}, form_path=detail).status_code == 409
    with monkeypatch.context() as patch:
        patch.setattr(allocations, "record_audit", fail_audit)
        with pytest.raises(RuntimeError, match="Simulated write failure"):
            post(manager, f"{detail}/cancel", reason, form_path=detail)
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "confirmed"
        assert db.session.query(RoomNight).filter_by(booking_id=booking_id).count() == 2
        assert db.session.query(PaymentRecord).filter_by(booking_id=booking_id).count() == 0
    assert post(manager, f"{detail}/cancel", reason, form_path=detail).status_code == 302
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "cancelled"
        assert db.session.query(PaymentRecord).filter_by(booking_id=booking_id).count() == 0


def test_report_deduplicates_maintenance_and_agrees_with_csv(app):
    _, manager_email = new_user(app, MANAGER)
    guest_id, _ = new_user(app, GUEST)
    start = TODAY - timedelta(days=5)
    label = "report-room"
    type_id, room_id = create_room(app, label, from_date=start)
    with app.app_context():
        customer_id = db.session.scalar(select(Customer.id).where(Customer.user_id == guest_id))
        completed = Booking(
            actor_user_id=guest_id, customer_id=customer_id, room_type_id=type_id, room_id=room_id,
            check_in=start, check_out=start + timedelta(days=2), guests=1, total=Decimal("400"),
            checked_in_at=datetime.combine(start, datetime.min.time()),
            checked_out_at=datetime.combine(start + timedelta(days=2), datetime.min.time()),
            status="checked_out", idempotency_key=str(uuid4()), request_digest="fixture",
        )
        future = Booking(
            actor_user_id=guest_id, customer_id=customer_id, room_type_id=type_id, room_id=room_id,
            check_in=TODAY, check_out=TODAY + timedelta(days=1), guests=1, total=Decimal("200"),
            status="confirmed", idempotency_key=str(uuid4()), request_digest="fixture",
        )
        db.session.add_all([completed, future])
        db.session.flush()
        completed_id = completed.id
        for offset in (0, 1):
            night = start + timedelta(days=offset)
            db.session.add(RoomNight(room_id=room_id, stay_date=night, booking_id=completed.id))
            db.session.add(BookingPriceLine(booking_id=completed.id, stay_date=night, amount=Decimal("200")))
        db.session.add(RoomNight(room_id=room_id, stay_date=TODAY, booking_id=future.id))
        db.session.add_all([
            RoomBlock(room_id=room_id, start_date=start + timedelta(days=1), end_date=start + timedelta(days=3), reason="Maintenance"),
            RoomBlock(room_id=room_id, start_date=start + timedelta(days=2), end_date=start + timedelta(days=4), reason="Overlapping"),
            RoomBlock(room_id=room_id, start_date=start + timedelta(days=4), end_date=TODAY, reason="Pending", effective=False),
        ])
        db.session.commit()
    manager, _ = sign_in(app, manager_email)
    completed_detail = f"/manage/bookings/{completed_id}"
    assert post(
        manager, f"{completed_detail}/cancel", {"source": "hotel", "reason": "Test"},
        form_path=completed_detail,
    ).status_code == 409
    query = {"period": "custom", "start": str(start), "end": str(TODAY)}
    page = manager.get("/manage/reports", query_string=query)
    assert page.status_code == 200 and "66.7%" in page.text
    export = re.search(r'href="(/manage/reports\.csv\?[^"]+)"', page.text)
    response = manager.get(unescape(export.group(1)))
    assert response.status_code == 200 and response.headers["Content-Type"].startswith("text/csv")
    row = next(row for row in csv.DictReader(response.data.decode("utf-8-sig").splitlines()) if row["Room type"] == label)
    assert row["Sellable past room nights"] == "3"
    assert row["Occupied room nights"] == "2"
    assert row["Occupancy (%)"] == "66.7"
    assert row["Upcoming reserved room nights"] == "1"
    assert row["Average confirmed nightly price (CNY)"] == "200.00"
    for period in ("week", "month", "quarter"):
        assert manager.get("/manage/reports", query_string={"period": period}).status_code == 200


def test_last_room_contention_and_idempotency_after_quote_changes(app):
    _, manager_email = new_user(app, MANAGER)
    _, first_email = new_user(app, GUEST)
    _, second_email = new_user(app, GUEST)
    room_type_id, _ = create_room(app, "contention-room")
    first, _ = sign_in(app, first_email)
    second, _ = sign_in(app, second_email)
    requests = [(client, booking_fields(client, room_type_id, TODAY)) for client in (first, second)]
    gate = Barrier(2)

    def reserve_concurrently(item):
        client, fields = item
        gate.wait(timeout=10)
        return client.post("/guest/book", data=fields)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(reserve_concurrently, requests))
    winner = next(index for index, response in enumerate(responses) if "/guest/bookings/" in response.location)
    loser = 1 - winner
    detail = responses[winner].location
    assert "/guest/bookings/" in detail and urlparse(responses[loser].location).path == "/guest/"
    winner_client, fields = requests[winner]
    manager, _ = sign_in(app, manager_email)
    changed = {
        "code": "contention-room", "name": "contention-room", "description": "Test room",
        "capacity": 2, "base_price": "500.00", "listed": "y", "bed_type": "double",
    }
    assert post(manager, f"/manage/room-types/{room_type_id}", changed).status_code == 302
    assert winner_client.post("/guest/book", data=fields).location == detail
    assert requests[loser][0].get(detail).status_code == 404
    with app.app_context():
        assert db.session.query(Booking).filter_by(room_type_id=room_type_id).count() == 1
