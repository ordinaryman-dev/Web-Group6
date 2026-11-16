"""A guest can take back a request the desk has not decided yet.

Withdrawing only moves the request row; the booking never changed while the request
was pending. The desk queue must lose the request at the same moment.
"""

import re
from datetime import date, timedelta

from app.booking import create_guest_request
from app.extensions import db
from app.models import BookingRequest, GUEST, STAFF
from test_frontdesk import make_room, new_user, post, reserve, sign_in

TODAY = date(2030, 6, 18)


def csrf_of(html):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)


def add_request(app, guest_id, booking_id, kind="dates"):
    with app.app_context():
        return create_guest_request(
            guest_id,
            booking_id,
            kind,
            {
                "check_in": str(TODAY + timedelta(days=4)),
                "check_out": str(TODAY + timedelta(days=6)),
            },
            "Move the stay",
        )


def test_a_guest_can_withdraw_and_the_desk_queue_drops_it(app):
    room_type_id, _ = make_room(app, "withdraw-room")
    guest_id, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)
    request_id = add_request(app, guest_id, booking_id)

    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    waiting = staff.get("/frontdesk/notifications.json").get_json()
    assert any(row["id"] == request_id for row in waiting["pending"])

    detail = f"/guest/bookings/{booking_id}"
    page = guest.get(detail).get_data(as_text=True)
    assert "Withdraw request" in page
    response = guest.post(
        f"{detail}/requests/{request_id}/withdraw",
        data={"csrf_token": csrf_of(page)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Request withdrawn" in response.text
    with app.app_context():
        assert db.session.get(BookingRequest, request_id).status == "withdrawn"

    after = staff.get("/frontdesk/notifications.json").get_json()
    assert all(row["id"] != request_id for row in after["pending"])
    assert after["counts"]["requests"] == waiting["counts"]["requests"] - 1


def test_withdrawn_requests_leave_the_withdraw_button_behind(app):
    room_type_id, _ = make_room(app, "withdraw-repeat")
    guest_id, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)
    request_id = add_request(app, guest_id, booking_id)

    detail = f"/guest/bookings/{booking_id}"
    page = guest.get(detail).get_data(as_text=True)
    guest.post(f"{detail}/requests/{request_id}/withdraw",
               data={"csrf_token": csrf_of(page)}, follow_redirects=True)

    page = guest.get(detail).get_data(as_text=True)
    assert "Withdraw request" not in page
    assert "withdrawn" in page


def test_another_guest_cannot_withdraw_the_request(app):
    room_type_id, _ = make_room(app, "withdraw-other")
    owner_id, owner_email = new_user(app, GUEST)
    owner = sign_in(app, owner_email)
    owner_booking = reserve(owner, room_type_id, TODAY + timedelta(days=1), nights=2)
    request_id = add_request(app, owner_id, owner_booking)

    _, other_email = new_user(app, GUEST)
    other = sign_in(app, other_email)
    other_booking = reserve(other, room_type_id, TODAY + timedelta(days=10), nights=2)

    detail = f"/guest/bookings/{other_booking}"
    page = other.get(detail).get_data(as_text=True)
    response = other.post(
        f"{detail}/requests/{request_id}/withdraw",
        data={"csrf_token": csrf_of(page)},
        follow_redirects=True,
    )

    assert "This request could not be found." in response.text
    with app.app_context():
        assert db.session.get(BookingRequest, request_id).status == "pending"


def test_a_request_the_desk_already_answered_cannot_be_withdrawn(app):
    room_type_id, _ = make_room(app, "withdraw-late")
    guest_id, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)
    request_id = add_request(app, guest_id, booking_id)

    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    post(staff, f"/frontdesk/requests/{request_id}/reject", {"req-comment": "Sold out"})

    detail = f"/guest/bookings/{booking_id}"
    page = guest.get(detail).get_data(as_text=True)
    response = guest.post(
        f"{detail}/requests/{request_id}/withdraw",
        data={"csrf_token": csrf_of(page)},
        follow_redirects=True,
    )

    assert "already been handled" in response.text
    with app.app_context():
        assert db.session.get(BookingRequest, request_id).status == "rejected"


def test_task_cards_name_their_request_so_a_stale_tab_can_grey_them(app):
    room_type_id, _ = make_room(app, "withdraw-stale")
    guest_id, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)
    request_id = add_request(app, guest_id, booking_id)

    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    page = staff.get("/frontdesk?view=tasks").get_data(as_text=True)

    assert f'data-request-id="{request_id}"' in page
