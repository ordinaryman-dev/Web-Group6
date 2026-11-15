"""A refused guest request must explain itself on the booking page.

Submitting the dates form without changing the dates used to flash a message at the
top of the page, which is easy to miss while the modal closes; the page now shows
the reason inside the actions panel. A real change must still go through.
"""

import re
from datetime import date, timedelta

from sqlalchemy import select

from app.extensions import db
from app.models import BookingRequest, GUEST
from test_frontdesk import make_room, new_user, reserve, sign_in

TODAY = date(2030, 6, 18)


def csrf_of(page_html):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html).group(1)


def submit_dates(guest, booking_id, check_in, check_out, reason="Change of plans"):
    detail = f"/guest/bookings/{booking_id}"
    page = guest.get(detail).get_data(as_text=True)
    return guest.post(
        f"{detail}/requests/dates",
        data={
            "csrf_token": csrf_of(page),
            "check_in": str(check_in),
            "check_out": str(check_out),
            "reason": reason,
        },
        follow_redirects=True,
    )


def test_unchanged_dates_are_refused_visibly(app):
    room_type_id, _ = make_room(app, "error-dates")
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    check_in = TODAY + timedelta(days=1)
    booking_id = reserve(guest, room_type_id, check_in, nights=2)

    response = submit_dates(guest, booking_id, check_in, check_in + timedelta(days=2))

    assert response.status_code == 200
    assert "Request not sent" in response.text
    assert "same as the current dates" in response.text
    with app.app_context():
        rows = db.session.scalars(
            select(BookingRequest).where(BookingRequest.booking_id == booking_id)
        ).all()
    assert rows == []


def test_a_real_change_is_still_accepted(app):
    room_type_id, _ = make_room(app, "error-dates-ok")
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    check_in = TODAY + timedelta(days=1)
    booking_id = reserve(guest, room_type_id, check_in, nights=2)

    response = submit_dates(guest, booking_id, check_in + timedelta(days=1),
                            check_in + timedelta(days=3))

    assert response.status_code == 200
    assert "Date-change request submitted" in response.text
    with app.app_context():
        rows = db.session.scalars(
            select(BookingRequest).where(BookingRequest.booking_id == booking_id)
        ).all()
    assert [row.status for row in rows] == ["pending"]


def test_the_dates_dialog_warns_before_the_request_is_sent(app):
    room_type_id, _ = make_room(app, "error-dates-markup")
    _, guest_email = new_user(app, GUEST)
    guest = sign_in(app, guest_email)
    check_in = TODAY + timedelta(days=1)
    booking_id = reserve(guest, room_type_id, check_in, nights=2)

    page = guest.get(f"/guest/bookings/{booking_id}").get_data(as_text=True)

    assert 'id="dates-form"' in page
    assert f'data-current-check-in="{check_in}"' in page
    assert f'data-current-check-out="{check_in + timedelta(days=2)}"' in page
    assert "data-dates-warning" in page
    assert 'id="dates-submit"' in page
    assert "Pick a different check-in or check-out first." in page
