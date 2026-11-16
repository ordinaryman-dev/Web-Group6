"""Cross-end gaps that belong to this task: a blacklisted guest cannot extend a
stay, and the front desk can read the floor plan without opening each room.
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
    assert "out-badge" in page.text
    assert "room out_of_service" in page.text
