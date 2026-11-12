from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.extensions import db
from app.models import PriceRule, Room, RoomBlock, RoomType


TODAY = date(2030, 6, 18)


def make_room_type(app, *, capacity=2, base_price="200.00", listed=True, rooms=2):
    with app.app_context():
        room_type = RoomType(
            code=f"search-{uuid4().hex[:8]}",
            name=f"Search test room {uuid4().hex[:6]}",
            description="Created by tests/test_search_api.py",
            capacity=capacity,
            base_price=Decimal(base_price),
            listed=listed,
        )
        db.session.add(room_type)
        db.session.flush()
        room_ids = []
        for _ in range(rooms):
            room = Room(
                number=f"T{uuid4().hex[:8].upper()}",
                room_type_id=room_type.id,
                operational_from=date(2020, 1, 1),
            )
            db.session.add(room)
            db.session.flush()
            room_ids.append(room.id)
        db.session.commit()
        return room_type.id, room_ids


def add_rule(app, room_type_id, kind, start, end, amount):
    with app.app_context():
        db.session.add(PriceRule(
            room_type_id=room_type_id, kind=kind, start_date=start, end_date=end,
            amount=Decimal(amount),
        ))
        db.session.commit()


def search(client, check_in=TODAY, check_out=date(2030, 6, 22), guests=2):
    return client.get("/api/search", query_string={
        "check_in": check_in.isoformat(), "check_out": check_out.isoformat(), "guests": guests,
    })


def offer(payload, room_type_id):
    return next(item for item in payload["room_types"] if item["id"] == room_type_id)


def test_search_reports_per_night_prices_totals_and_counts(app):
    room_type_id, _ = make_room_type(app)
    client = app.test_client()

    response = search(client)
    assert response.status_code == 200
    payload = response.json
    assert (payload["check_in"], payload["check_out"], payload["guests"]) == ("2030-06-18", "2030-06-22", 2)

    item = offer(payload, room_type_id)
    assert item["capacity"] == 2
    assert item["available_count"] == 2
    assert item["nights"] == [
        {"date": "2030-06-18", "price": "200.00"},
        {"date": "2030-06-19", "price": "200.00"},
        {"date": "2030-06-20", "price": "200.00"},
        {"date": "2030-06-21", "price": "200.00"},
    ]
    assert item["total"] == "800.00"
    assert "room_id" not in item and "room_number" not in item


def test_search_applies_price_rules_per_night(app):
    room_type_id, _ = make_room_type(app, base_price="200.00")
    add_rule(app, room_type_id, "season", date(2030, 6, 18), date(2030, 6, 21), "250.00")
    add_rule(app, room_type_id, "date", date(2030, 6, 19), date(2030, 6, 20), "400.00")
    client = app.test_client()

    item = offer(search(client).json, room_type_id)
    assert [night["price"] for night in item["nights"]] == ["250.00", "400.00", "250.00", "200.00"]
    assert item["total"] == "1100.00"


def test_search_hides_unlisted_types_and_respects_capacity(app):
    room_type_id, _ = make_room_type(app, listed=False)
    client = app.test_client()

    assert all(item["id"] != room_type_id for item in search(client).json["room_types"])
    assert all(item["id"] != room_type_id for item in search(client, guests=1).json["room_types"])


def test_search_counts_only_rooms_free_for_the_whole_stay(app):
    room_type_id, room_ids = make_room_type(app, rooms=2)
    client = app.test_client()

    assert offer(search(client, date(2030, 6, 18), date(2030, 6, 20)).json, room_type_id)["available_count"] == 2

    with app.app_context():
        db.session.add(RoomBlock(
            room_id=room_ids[0], start_date=date(2030, 6, 19), end_date=date(2030, 6, 20), reason="Maintenance",
        ))
        db.session.add(RoomBlock(
            room_id=room_ids[1], start_date=date(2030, 6, 18), end_date=date(2030, 6, 19), reason="Maintenance",
        ))
        db.session.commit()

    # Either block alone leaves one room free; together no single room covers the stay.
    assert offer(search(client, date(2030, 6, 18), date(2030, 6, 19)).json, room_type_id)["available_count"] == 1
    assert all(
        item["id"] != room_type_id
        for item in search(client, date(2030, 6, 18), date(2030, 6, 20)).json["room_types"]
    )


def test_search_enforces_the_arrival_window(app):
    client = app.test_client()

    assert search(client, TODAY, date(2030, 6, 20)).status_code == 200
    assert search(client, date(2030, 7, 2), date(2030, 7, 4)).status_code == 200
    late = search(client, date(2030, 7, 3), date(2030, 7, 5))
    assert late.status_code == 400
    assert late.json["code"] == "INVALID_REQUEST"


def test_search_rejects_malformed_requests(app):
    client = app.test_client()

    assert search(client, date(2030, 6, 22), date(2030, 6, 22)).status_code == 400
    assert search(client, guests=0).status_code == 400
    bad_date = client.get("/api/search", query_string={
        "check_in": "not-a-date", "check_out": "2030-06-22", "guests": 2,
    })
    assert bad_date.status_code == 400
    missing = client.get("/api/search", query_string={"guests": 2})
    assert missing.status_code == 400
