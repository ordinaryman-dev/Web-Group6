import re
from datetime import timedelta
from html import unescape
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from app.extensions import db
from app.models import Room, RoomBlock, RoomType, STAFF, User
from test_manager import TODAY, new_user, sign_in


def select_link(response, label):
    match = re.search(r'<a[^>]*href="([^"]+)">' + re.escape(label) + "</a>", response.text)
    assert match, label
    return unescape(match.group(1))


def test_room_inventory_paginates_and_searches_all_floors(app):
    _, email = new_user(app, "manager")
    client, _ = sign_in(app, email)
    with app.app_context():
        room_type = RoomType(
            code="pager-test", name="Pagination Twin", description="Integration inventory",
            capacity=2, bed_type="twin", base_price=120,
        )
        db.session.add(room_type)
        db.session.flush()
        inventory = [
            Room(number=f"{floor}{number:02}", room_type_id=room_type.id, operational_from=TODAY,
                 housekeeping="dirty" if (floor, number) == (12, 24) else "clean",
                 has_window=number != 24)
            for floor in range(1, 13) for number in range(1, 26)
        ]
        db.session.add_all(inventory)
        db.session.flush()
        db.session.add(RoomBlock(
            room_id=inventory[-1].id, start_date=TODAY, end_date=TODAY + timedelta(days=1),
            reason="Lift repair",
        ))
        target = inventory[-1].id
        db.session.commit()

    page_one = client.get("/manage/rooms?q=Pagination+Twin")
    assert page_one.status_code == 200
    assert re.findall(r'data-room-number="([^"]+)"', page_one.text) == [str(number) for number in range(101, 126)]
    assert "of 300" in page_one.text and "<span>1F</span>" in page_one.text
    page_two = client.get(select_link(page_one, "Next"))
    assert re.findall(r'data-room-number="([^"]+)"', page_two.text) == [str(number) for number in range(201, 226)]
    last = client.get("/manage/rooms?q=Pagination+Twin&sort=number_desc&floor=12&per_page=50")
    assert re.findall(r'data-room-number="([^"]+)"', last.text) == [str(number) for number in range(1225, 1200, -1)]
    dirty = client.get("/manage/rooms?q=Pagination+Twin&status=dirty&bed=twin&window=without")
    assert re.findall(r'data-room-number="([^"]+)"', dirty.text) == ["1224"]
    blocked = client.get("/manage/rooms?q=1225&status=maintenance&floor=12")
    assert re.findall(r'data-room-number="([^"]+)"', blocked.text) == ["1225"]
    assert "manager-status maintenance" in blocked.text
    editor = client.get(select_link(blocked, "Edit"))
    back = urlsplit(select_link(editor, "Back"))
    assert back.path == "/manage/rooms"
    assert parse_qs(back.query) == {"q": ["1225"], "status": ["maintenance"], "floor": ["12"]}
    with app.app_context():
        assert db.session.scalar(select(Room.id).where(Room.number == "1225")) == target


def test_staff_search_sort_and_pagination(app):
    _, email = new_user(app, "manager")
    client, _ = sign_in(app, email)
    with app.app_context():
        db.session.add_all(
            User(email=f"pagination-staff-{number:02}@example.test", role=STAFF,
                 active=number != 54, password_hash="unused")
            for number in range(55)
        )
        db.session.commit()
    query = "/manage/accounts?q=pagination-staff&sort=email_desc"
    first = client.get(query)
    assert first.status_code == 200
    assert "of 55" in first.text
    assert "pagination-staff-54@example.test" in first.text and "pagination-staff-29@example.test" not in first.text
    page_two = client.get(select_link(first, "Next"))
    assert "pagination-staff-29@example.test" in page_two.text
    assert "pagination-staff-54@example.test" not in page_two.text
    inactive = client.get(query + "&status=inactive&per_page=999")
    assert "1–1 of 1" in inactive.text and 'value="25" selected' in inactive.text
