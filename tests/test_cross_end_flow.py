# 客户、员工和管理员端到端流程：确认三个页面读写同一份订单、房晚与付款记录。
"""One guest request, seen by the front desk, decided, and read back by the guest.

This is the whole point of the cross-end work: the same row has to appear in the
guest's history, the desk's queue and the polling feeds, and the desk's decision has
to come back to the guest without anyone reloading by hand.
"""

import re
from datetime import date, timedelta

from sqlalchemy import select

from app.extensions import db
from app.models import BookingRequest, GUEST, STAFF
from test_frontdesk import make_room, new_user, post, reserve, sign_in

TODAY = date(2030, 6, 18)


def csrf_of(html):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)


def test_a_guest_request_travels_to_the_desk_and_back(app):
    # 1. the guest books a room
    room_type_id, _ = make_room(app, "flow-room")
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)
    detail = f"/guest/bookings/{booking_id}"

    # 2. the guest asks for new dates
    page = guest.get(detail).get_data(as_text=True)
    submitted = guest.post(
        f"{detail}/requests/dates",
        data={
            "csrf_token": csrf_of(page),
            "check_in": str(TODAY + timedelta(days=2)),
            "check_out": str(TODAY + timedelta(days=4)),
            "reason": "Flight moved a day later",
        },
        follow_redirects=True,
    )
    assert "Date-change request submitted" in submitted.text
    with app.app_context():
        request_id = db.session.scalar(
            select(BookingRequest.id).where(BookingRequest.booking_id == booking_id)
        )
        assert db.session.get(BookingRequest, request_id).status == "pending"

    # 3. the front desk sees the same row, in the feed and on the tasks page
    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    feed = staff.get("/frontdesk/notifications.json").get_json()
    assert any(row["id"] == request_id for row in feed["pending"])
    tasks = staff.get("/frontdesk?view=tasks")
    assert f'data-request-id="{request_id}"' in tasks.text

    # 4. the desk refuses it and gives a reason
    refused = post(staff, f"/frontdesk/requests/{request_id}/reject",
                   {"req-comment": "Sold out for those nights"})
    assert refused.status_code == 302

    # 5. the decision comes back to the guest, through the feed and the page
    status = guest.get("/guest/status.json").get_json()
    assert status["requests"][str(request_id)] == "rejected"
    guest_page = guest.get(detail).get_data(as_text=True)
    assert "rejected" in guest_page
    assert "Sold out for those nights" in guest_page

    # 6. and the desk queue is empty again
    after = staff.get("/frontdesk/notifications.json").get_json()
    assert all(row["id"] != request_id for row in after["pending"])


def test_the_guest_status_feed_is_private(app):
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    own = guest.get("/guest/status.json")
    assert own.status_code == 200
    assert "version" in own.get_json()

    anonymous = app.test_client().get("/guest/status.json")
    assert anonymous.status_code in (302, 401, 403)
