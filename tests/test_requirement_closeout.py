"""Regression checks for walk-in identity, staff profile and terminal requests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash

from app import desk
from app.booking import Stay, cancel_confirmed_booking
from app.extensions import db
from app.models import (
    AuditLog, BlacklistEntry, Booking, BookingRequest, Customer, CustomerPhoneLock,
    GUEST, MANAGER, Room, STAFF, User, utc_now,
)
from test_frontdesk import new_user, post, sign_in
from test_frontdesk_rules import TODAY, NOW, make_booking, make_type_and_rooms


PASSWORD = "A password for integration tests"


@pytest.fixture
def records(app):
    staff, email = new_user(app, STAFF)
    other_staff, _ = new_user(app, STAFF)
    manager, _ = new_user(app, MANAGER)
    kind, rooms = make_type_and_rooms(app, "close-" + uuid4().hex[:7])
    number = f"139{uuid4().int % 100000000:08d}"
    with app.app_context():
        customer = Customer(name="Existing guest", phone=f"+86 ({number[:3]}) {number[3:7]}-{number[7:]}")
        db.session.add(customer)
        db.session.commit()
        return dict(staff=staff, email=email, other_staff=other_staff, manager=manager,
                    kind=kind, rooms=rooms, customer=customer.id, number="+86" + number,
                    new_phone=f"137{uuid4().int % 100000000:08d}")


def open_walkin(session, data, **values):
    return desk.create_walkin(session, data["staff"], data["kind"],
                              Stay(TODAY, TODAY + timedelta(days=1), 1), **values)


def request_for(app, booking_id, actor, kind="dates"):
    with app.app_context():
        row = BookingRequest(booking_id=booking_id, submitted_by=actor, kind=kind,
                             reason="Guest change", status="pending", target={
                                 "check_in": str(TODAY + timedelta(days=1)),
                                 "check_out": str(TODAY + timedelta(days=3)),
                             })
        db.session.add(row)
        db.session.commit()
        return row.id


def test_walkin_matching_phone_requires_selection_and_preserves_existing_details(app, records):
    data = records
    with app.app_context():
        with pytest.raises(desk.ExistingCustomerRequired):
            with Session(db.engine) as session, session.begin():
                open_walkin(session, data, name="Different name", phone=data["number"])
        assert db.session.query(Customer).filter(
            desk.normalized_customer_phone() == data["number"]).count() == 1
        assert db.session.get(Customer, data["customer"]).name == "Existing guest"
        assert db.session.query(Booking).filter_by(room_type_id=data["kind"]).count() == 0
        assert [row.id for row in desk.find_customers(db.session, data["number"])] == [data["customer"]]


def test_walkin_duplicate_phone_redirects_to_restricted_existing_guest(app, records):
    data = records
    with app.app_context():
        db.session.add(BlacklistEntry(customer_id=data["customer"], actor_user_id=data["manager"], reason="Restricted"))
        db.session.commit()
    client = sign_in(app, data["email"])
    response = post(client, "/frontdesk/walk-in", {
        "walk-name": "New guest", "walk-phone": data["number"], "walk-room_id": data["rooms"][0],
        "walk-room_type_id": data["kind"], "walk-check_in": str(TODAY),
        "walk-check_out": str(TODAY + timedelta(days=1)), "walk-guests": 1,
    }, form_path=f"/frontdesk?room={data['rooms'][0]}")
    assert response.status_code == 302 and "lookup=" in response.location
    page = client.get(response.location)
    assert "Restricted · no new walk-in" in page.text
    with app.app_context():
        assert db.session.query(Booking).filter_by(room_type_id=data["kind"]).count() == 0
        with pytest.raises(desk.DeskError, match="blacklisted"):
            with Session(db.engine) as session, session.begin():
                open_walkin(session, data, customer_id=data["customer"])


def test_shared_phone_does_not_merge_distinct_selected_customer_ids(app, records):
    data = records
    with app.app_context():
        second = Customer(name="Another household member", phone=data["number"])
        db.session.add_all([second, BlacklistEntry(customer_id=data["customer"],
                                                 actor_user_id=data["manager"], reason="Restricted")])
        db.session.commit()
        second_id = second.id
        with Session(db.engine) as session, session.begin():
            row = open_walkin(session, data, customer_id=second_id)
            assert row.customer_id == second_id
        assert db.session.get(Customer, data["customer"]).name == "Existing guest"


def test_concurrent_walkin_registration_creates_one_customer(app, records):
    data = records
    gate = Barrier(2)

    def register(actor):
        with app.app_context():
            engine = db.engine.execution_options(isolation_level="READ COMMITTED")
            gate.wait(timeout=10)
            try:
                with Session(engine) as session, session.begin():
                    desk.create_walkin(session, actor, data["kind"],
                                       Stay(TODAY, TODAY + timedelta(days=1), 1),
                                       name="New guest", phone=data["new_phone"])
                return "created"
            except desk.ExistingCustomerRequired:
                return "select_existing"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register, (data["staff"], data["other_staff"]))) == ["created", "select_existing"]
    with app.app_context():
        assert db.session.query(Customer).filter_by(phone=data["new_phone"]).count() == 1
        assert db.session.query(Booking).filter_by(room_type_id=data["kind"]).count() == 1


def test_failed_new_walkin_rolls_back_customer_and_registration_lock(app, records):
    data = records
    with app.app_context():
        db.session.get(Room, data["rooms"][0]).operational_until = TODAY
        db.session.commit()
        before = db.session.query(CustomerPhoneLock).count()
        with pytest.raises(desk.DeskError, match="No room"):
            with Session(db.engine) as session, session.begin():
                open_walkin(session, data, room_id=data["rooms"][0], name="New guest", phone=data["new_phone"])
        db.session.rollback()
        assert db.session.query(Customer).filter_by(phone=data["new_phone"]).count() == 0
        assert db.session.query(CustomerPhoneLock).count() == before


def test_staff_can_change_only_their_own_password_with_current_password(app, records):
    data = records
    client = sign_in(app, data["email"])
    assert 'href="/frontdesk/profile"' in client.get("/frontdesk").text
    profile = "/frontdesk/profile"
    assert client.get(profile).status_code == 200
    new_password = "A new staff password for testing"
    fields = {"current_password": "wrong", "new_password": new_password, "confirm": new_password}
    assert post(client, profile, fields, form_path=profile).status_code == 400
    with app.app_context():
        assert check_password_hash(db.session.get(User, data["staff"]).password_hash, PASSWORD)
    changed = post(client, profile, {**fields, "current_password": PASSWORD, "user_id": data["other_staff"]}, form_path=profile)
    assert changed.status_code == 302
    with app.app_context():
        assert check_password_hash(db.session.get(User, data["staff"]).password_hash, new_password)
        assert check_password_hash(db.session.get(User, data["other_staff"]).password_hash, PASSWORD)
        audits = db.session.scalars(select(AuditLog).where(
            AuditLog.action == "change_password", AuditLog.actor_user_id == data["staff"],
        )).all()
        assert len(audits) == 1 and audits[0].details is None
    assert sign_in(app, data["email"], new_password).get(profile).status_code == 200


def test_guest_cannot_open_staff_profile(app):
    _, email = new_user(app, GUEST)
    assert sign_in(app, email).get("/frontdesk/profile").status_code == 403


@pytest.mark.parametrize("status", ["confirmed", "checked_in"])
def test_reserved_and_occupied_rooms_offer_maintenance_with_conflict_preview(app, records, status):
    data = records
    booking_id = make_booking(app, data["kind"], data["rooms"], status=status)
    client = sign_in(app, data["email"])
    page = client.get(f"/frontdesk?booking={booking_id}")
    assert page.status_code == 200
    assert 'action="/frontdesk/blocks"' in page.text and 'data-block-preview=' in page.text
    result = post(client, "/frontdesk/blocks", {
        "block-room_id": data["rooms"][0], "block-start_date": str(TODAY),
        "block-end_date": str(TODAY + timedelta(days=1)), "block-reason": "Repair",
    }, form_path=f"/frontdesk?booking={booking_id}")
    assert result.status_code == 302
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == status
        assert db.session.get(Booking, booking_id).room_id == data["rooms"][0]


@pytest.mark.parametrize("action", ["no_show", "check_out", "cancel"])
def test_terminal_actions_close_pending_requests_atomically(app, records, action):
    data = records
    booking_id = make_booking(app, data["kind"], data["rooms"],
                              status="checked_in" if action == "check_out" else "confirmed", expected=time(14))
    request_id = request_for(app, booking_id, data["staff"])
    with app.app_context():
        booking = db.session.get(Booking, booking_id)
        booking.last_contact_at, booking.last_contact_result = utc_now(), "no_answer"
        db.session.commit()
        with Session(db.engine) as session, session.begin():
            if action == "cancel":
                cancel_confirmed_booking(session, data["manager"], booking_id, "hotel", "Hotel cancellation", NOW)
            elif action == "no_show":
                desk.mark_no_show(session, booking_id, data["staff"], "No answer", NOW)
            else:
                desk.check_out(session, booking_id, data["staff"], NOW)
        db.session.rollback()
        row = db.session.get(BookingRequest, request_id)
        assert row.status == "closed" and row.handled_at and row.handled_by
        assert "Closed because" in row.comment
        assert request_id not in [item.id for item in desk.pending_requests(db.session)]
        assert db.session.query(AuditLog).filter_by(action="close_request", object_id=request_id).count() == 1


def test_request_close_failure_rolls_back_the_whole_terminal_action(app, records, monkeypatch):
    from app import booking as booking_service
    data = records
    booking_id = make_booking(app, data["kind"], data["rooms"], status="checked_in")
    request_id = request_for(app, booking_id, data["staff"])

    def fail(*args, **kwargs):
        raise RuntimeError("Request audit failed")

    with app.app_context(), monkeypatch.context() as patch:
        patch.setattr(booking_service, "record_audit", fail)
        with pytest.raises(RuntimeError, match="Request audit failed"):
            with Session(db.engine) as session, session.begin():
                desk.check_out(session, booking_id, data["staff"], NOW)
        assert db.session.get(Booking, booking_id).status == "checked_in"
        assert db.session.get(BookingRequest, request_id).status == "pending"
        assert db.session.get(Room, data["rooms"][0]).housekeeping == "clean"


def test_overdue_occupied_room_is_a_maintenance_conflict_without_a_move_button(app, records):
    data = records
    booking_id = make_booking(app, data["kind"], data["rooms"],
                              check_in=TODAY - timedelta(days=5), status="checked_in")
    client = sign_in(app, data["email"])
    dates = {"room_id": data["rooms"][0], "start_date": str(TODAY),
             "end_date": str(TODAY + timedelta(days=1))}
    preview = client.get("/frontdesk/blocks/preview", query_string=dates)
    assert preview.status_code == 200 and preview.json["count"] == 1
    response = post(client, "/frontdesk/blocks", {
        "block-room_id": dates["room_id"], "block-start_date": dates["start_date"],
        "block-end_date": dates["end_date"], "block-reason": "Repair",
    }, form_path=f"/frontdesk?booking={booking_id}")
    page = client.get(response.location)
    assert "Manager coordination required" in page.text
    assert f'/frontdesk/bookings/{booking_id}/reassign' not in page.text
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "checked_in"


def test_no_show_and_approval_have_one_booking_then_request_lock_order(app, records):
    data = records
    booking_id = make_booking(app, data["kind"], data["rooms"], expected=time(14))
    request_id = request_for(app, booking_id, data["staff"])
    with app.app_context():
        row = db.session.get(Booking, booking_id)
        row.last_contact_at, row.last_contact_result = utc_now(), "no_answer"
        db.session.commit()
    gate = Barrier(2)

    def run(action):
        with app.app_context():
            gate.wait(timeout=10)
            try:
                with Session(db.engine.execution_options(isolation_level="READ COMMITTED")) as session, session.begin():
                    if action == "no_show":
                        desk.mark_no_show(session, booking_id, data["staff"], "No answer", NOW)
                    else:
                        desk.approve_request(session, request_id, data["other_staff"], "Approved", today=TODAY, now=NOW)
                return "done"
            except desk.DeskError:
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert "done" in list(pool.map(run, ("no_show", "approve")))
    with app.app_context():
        row = db.session.get(BookingRequest, request_id)
        assert row.status in ("closed", "approved")
