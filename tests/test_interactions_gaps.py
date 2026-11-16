"""The remaining interaction gaps: window flag, guest profile, staff recovery,
blacklisted extensions, outage preview and the front-desk floor plan.
"""

import re
from datetime import date, timedelta

from sqlalchemy import select

from app.extensions import db
from app.models import BookingRequest, Customer, GUEST, Room, STAFF, User
from test_frontdesk import make_room, new_user, post, reserve, sign_in
from test_manager import PASSWORD, post as manager_post, new_user as manager_user, sign_in as manager_sign_in

TODAY = date(2030, 6, 18)


def csrf_of(html):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)


# ---------------------------------------------------------------- P1-4 windows
def test_the_room_form_can_set_the_window_flag(app):
    _, manager_email = manager_user(app, "manager")
    manager, _ = manager_sign_in(app, manager_email)
    with app.app_context():
        from app.models import RoomType

        room_type = RoomType(code="win-room", name="Window room", description="test",
                             capacity=2, base_price=200)
        db.session.add(room_type)
        db.session.commit()
        type_id = room_type.id

    page = manager.get("/manage/rooms/new")
    assert "Has a window" in page.text

    room = {"number": "W-101", "room_type_id": type_id, "operational_from": str(TODAY),
            "operational_until": "", "has_window": "y"}
    assert manager_post(manager, "/manage/rooms/new", room).status_code == 302
    with app.app_context():
        created = db.session.scalar(select(Room).where(Room.number == "W-101"))
        assert created.has_window is True
        room_id = created.id

    edit = {"number": "W-101", "room_type_id": type_id, "operational_from": str(TODAY),
            "operational_until": ""}
    assert manager_post(manager, f"/manage/rooms/{room_id}", edit).status_code == 302
    with app.app_context():
        assert db.session.get(Room, room_id).has_window is False


# ---------------------------------------------------------------- P1-5 profile
def test_a_guest_can_update_name_and_phone(app):
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)

    page = guest.get("/guest/account")
    assert "Contact details" in page.text

    response = guest.post(
        "/guest/account/profile",
        data={"csrf_token": csrf_of(page.text), "name": "New Name", "phone": "13900001111"},
        follow_redirects=True,
    )
    assert "contact details were updated" in response.text
    with app.app_context():
        user_id = db.session.scalar(select(User.id).where(User.email == guest_email))
        customer = db.session.scalar(select(Customer).where(Customer.user_id == user_id))
        assert (customer.name, customer.phone) == ("New Name", "13900001111")


def test_a_bad_phone_number_is_refused(app):
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    page = guest.get("/guest/account")
    response = guest.post(
        "/guest/account/profile",
        data={"csrf_token": csrf_of(page.text), "name": "Still Here", "phone": "abc"},
        follow_redirects=True,
    )
    assert "Enter a name and a phone number" in response.text


# ---------------------------------------------------------------- P1-7 staff
def test_a_disabled_staff_account_can_be_enabled_again(app):
    _, manager_email = manager_user(app, "manager")
    manager, _ = manager_sign_in(app, manager_email)
    staff_id, staff_email = manager_user(app, STAFF)

    assert manager_post(manager, f"/manage/accounts/{staff_id}/disable", {},
                        form_path="/manage/accounts").status_code == 302
    with app.app_context():
        assert db.session.get(User, staff_id).active is False

    assert manager_post(manager, f"/manage/accounts/{staff_id}/enable", {},
                        form_path="/manage/accounts").status_code == 302
    with app.app_context():
        assert db.session.get(User, staff_id).active is True


def test_a_manager_can_reset_a_staff_password(app):
    _, manager_email = manager_user(app, "manager")
    manager, _ = manager_sign_in(app, manager_email)
    staff_id, staff_email = manager_user(app, STAFF)

    page = manager.get(f"/manage/accounts/{staff_id}/password")
    assert page.status_code == 200
    response = manager.post(
        f"/manage/accounts/{staff_id}/password",
        data={"csrf_token": csrf_of(page.text), "password": "Brand-new-pass-1"},
    )
    assert response.status_code == 302

    client = app.test_client()
    login = client.get("/login")
    assert client.post("/login", data={
        "email": staff_email, "password": "Brand-new-pass-1",
        "csrf_token": csrf_of(login.text),
    }).status_code == 302


# ---------------------------------------------------------------- P1-10 blocked
def test_a_blacklisted_guest_cannot_extend(app):
    room_type_id, room_id = make_room(app, "blocked-extend")
    guest_id, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)

    with app.app_context():
        customer_id = db.session.scalar(
            select(Customer.id).where(Customer.user_id == guest_id)
        )
        from app.models import BlacklistEntry
        from app.models import utc_now

        db.session.add(BlacklistEntry(customer_id=customer_id, actor_user_id=guest_id,
                                      reason="Payment fraud", effective_at=utc_now()))
        db.session.commit()

    detail = f"/guest/bookings/{booking_id}"
    page = guest.get(detail)
    check_out = TODAY + timedelta(days=5)
    response = guest.post(
        f"{detail}/requests/extension",
        data={"csrf_token": csrf_of(page.text), "check_out": str(check_out), "reason": "Stay longer"},
        follow_redirects=True,
    )
    assert "blacklisted" in response.text
    with app.app_context():
        rows = db.session.scalars(
            select(BookingRequest).where(BookingRequest.booking_id == booking_id)
        ).all()
    assert rows == []


# ---------------------------------------------------------------- P1-1 preview
def test_the_outage_form_previews_booking_conflicts(app):
    room_type_id, room_id = make_room(app, "preview-room")
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    reserve(guest, room_type_id, TODAY + timedelta(days=2), nights=2)

    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    start = TODAY + timedelta(days=2)
    response = staff.get("/frontdesk/blocks/preview", query_string={
        "room_id": room_id, "start": str(start), "end": str(start + timedelta(days=1)),
    })
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["conflicts"] == 1
    assert payload["bookings"][0].startswith("BK-")

    clear = staff.get("/frontdesk/blocks/preview", query_string={
        "room_id": room_id, "start": str(start + timedelta(days=20)),
        "end": str(start + timedelta(days=21)),
    })
    assert clear.get_json()["conflicts"] == 0


# ---------------------------------------------------------------- plan view
def test_the_front_desk_has_a_floor_plan_view(app):
    make_room(app, "plan-room")
    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)

    page = staff.get("/frontdesk?view=plan")
    assert page.status_code == 200
    assert 'class="plan-svg"' in page.text
    assert "Tonight’s plan" in page.text
    assert 'data-notify-feed="/frontdesk/notifications.json"' in page.text


def test_a_blocked_room_is_marked_red_on_the_plan(app):
    from uuid import uuid4

    room_type_id, room_id = make_room(app, "plan-blocked")
    floor = "97"
    with app.app_context():
        from app.models import Room, RoomBlock

        room = db.session.get(Room, room_id)
        room.number = f"{floor}{uuid4().int % 90 + 10}"
        db.session.add(RoomBlock(room_id=room_id, start_date=TODAY,
                                 end_date=TODAY + timedelta(days=2),
                                 reason="Bathroom leak", created_by_id=None))
        db.session.commit()

    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    page = staff.get(f"/frontdesk?view=plan&plan_floor={floor}")

    assert page.status_code == 200
    assert "OUT OF SERVICE" in page.text
    assert "room out_of_service" in page.text
