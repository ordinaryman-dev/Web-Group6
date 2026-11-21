# 员工通知：验证待办数字、轮询版本及页面展示保持一致。
"""The front-desk bell and the JSON feed behind it.

The bell refreshes by polling ``/frontdesk/notifications.json``. The page and the
feed share one snapshot function, so these tests pin the contract: the same counts,
a version that moves when something changes, and a guest request that really
reaches the desk and leaves again once it is decided.
"""

import re
from datetime import date, timedelta

from app.extensions import db
from app.models import Booking, BookingRequest, GUEST, STAFF
from test_frontdesk import make_room, new_user, post, reserve, sign_in

TODAY = date(2030, 6, 18)


def add_request(app, booking_id, guest_id, kind="dates"):
    with app.app_context():
        row = BookingRequest(
            booking_id=booking_id,
            kind=kind,
            target={"check_in": str(TODAY + timedelta(days=4)),
                    "check_out": str(TODAY + timedelta(days=6))},
            reason="Move the stay",
            submitted_by=guest_id,
            status="pending",
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def feed(client):
    response = client.get("/frontdesk/notifications.json")
    assert response.status_code == 200
    assert response.is_json
    return response.get_json()


def test_a_guest_request_reaches_the_front_desk(app):
    room_type_id, _ = make_room(app, "bell-room")
    guest_id, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)
    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)

    before = feed(staff)
    add_request(app, booking_id, guest_id)

    after = feed(staff)
    assert after["counts"]["requests"] == before["counts"]["requests"] + 1
    assert after["version"] != before["version"]
    assert after["total"] >= after["counts"]["requests"]

    titles = " | ".join(item["title"] for item in after["items"])
    assert re.search(r"\d+ pending requests?", titles), titles
    entry = next(item for item in after["items"] if item["kind"] == "requests")
    assert entry["href"].startswith("/frontdesk") and entry["tone"] == "amber"

    with app.app_context():
        reference = db.session.get(Booking, booking_id).reference
    assert after["pending"][0]["booking"] == reference


def test_a_decision_clears_the_request_from_the_bell(app):
    room_type_id, _ = make_room(app, "bell-decided")
    guest_id, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    booking_id = reserve(guest, room_type_id, TODAY + timedelta(days=1), nights=2)
    request_id = add_request(app, booking_id, guest_id)
    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)

    waiting = feed(staff)
    assert waiting["counts"]["requests"] >= 1
    assert any(row["id"] == request_id for row in waiting["pending"])

    response = post(staff, f"/frontdesk/requests/{request_id}/reject", {"req-comment": "Sold out"})
    assert response.status_code == 302

    after = feed(staff)
    assert after["counts"]["requests"] == waiting["counts"]["requests"] - 1
    assert all(row["id"] != request_id for row in after["pending"])
    assert after["version"] != waiting["version"]


def test_the_feed_is_staff_only(app):
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    assert guest.get("/frontdesk/notifications.json").status_code == 403


def test_the_desk_page_subscribes_to_the_feed(app):
    _, staff_email = new_user(app, STAFF)
    staff = sign_in(app, staff_email)
    page = staff.get("/frontdesk?view=tasks")
    assert page.status_code == 200
    assert 'data-notify-feed="/frontdesk/notifications.json"' in page.text
    assert "data-notify-version=" in page.text
    assert 'data-notify-count="requests"' in page.text
    assert 'data-notify-total' in page.text
