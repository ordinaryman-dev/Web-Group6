from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.extensions import db
from app.models import Booking, PriceRule, Room, RoomBlock, RoomNight, RoomType


def quote(client, check_in="2026-11-12", check_out="2026-11-15", guests=2):
    return client.get("/api/availability", query_string={
        "check_in": check_in, "check_out": check_out, "guests": guests,
    })


def test_catalogue_search_and_exact_price_priority(demo, client):
    with demo.app_context():
        single = db.session.scalar(select(RoomType).where(RoomType.code == "demo-single"))
        single.listed = False
        db.session.commit()

    home = client.get("/")
    assert b"Comfort Double" in home.data and b"Classic Single" not in home.data
    response = quote(client)
    assert response.status_code == 200
    types = response.json["room_types"]
    assert {item["name"] for item in types} == {"Comfort Double", "Family Suite"}
    double = next(item for item in types if item["name"] == "Comfort Double")
    assert double["total"] == "1430.00"
    assert [rate["amount"] for rate in double["rates"]] == ["460.00", "510.00", "460.00"]
    assert "room_id" not in double and "room_number" not in double
    assert quote(client, guests=5).json["room_types"] == []
    fallback = client.get("/?check_in=2026-11-12&check_out=2026-11-15&guests=2")
    assert fallback.status_code == 200 and "1430.00" in fallback.get_data(as_text=True)
    with demo.app_context():
        assert db.session.scalar(select(func.count()).select_from(Booking)) == 0
        assert db.session.scalar(select(func.count()).select_from(RoomNight)) == 0


def test_arrival_window_and_beijing_cutoff(demo, client):
    assert quote(client, check_in="2026-11-25", check_out="2026-12-02").status_code == 200
    assert quote(client, check_in="2026-11-26", check_out="2026-12-02").status_code == 400
    assert quote(client, check_in="2026-11-12", check_out="2026-11-12").status_code == 400
    assert quote(client, check_in="2026-11-11", guests=0).status_code == 400
    assert quote(client, check_in="not-a-date").status_code == 400
    demo.config["HOTEL_CLOCK"] = lambda: datetime(2026, 11, 11, 22, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert quote(client, check_in="2026-11-11").status_code == 400
    assert quote(client, check_in="2026-11-12").status_code == 200


def test_requires_one_room_for_entire_stay_and_uses_exclusive_checkout(demo, client):
    start, middle, end = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 14)
    with demo.app_context():
        first = db.session.scalar(select(Room).where(Room.number == "D201"))
        second = db.session.scalar(select(Room).where(Room.number == "D202"))
        db.session.add_all([
            RoomBlock(room_id=first.id, start_date=start, end_date=middle, reason="Maintenance"),
            RoomBlock(room_id=second.id, start_date=middle, end_date=end, reason="Maintenance"),
        ])
        db.session.commit()
    assert {item["name"] for item in quote(client, check_out=end).json["room_types"]} == {"Family Suite"}
    assert "Comfort Double" in {
        item["name"] for item in quote(client, check_out=middle).json["room_types"]
    }
    with demo.app_context():
        db.session.scalar(select(Room).where(Room.number == "D201")).operational_until = middle
        db.session.commit()
    assert "Comfort Double" not in {
        item["name"] for item in quote(client, check_in=middle, check_out=end).json["room_types"]
    }


def test_seed_can_be_run_again_without_duplicates(demo):
    result = demo.test_cli_runner().invoke(args=["seed-demo"])
    assert result.exit_code == 0, result.output
    with demo.app_context():
        assert db.session.scalar(select(func.count()).select_from(RoomType)) == 3
        assert db.session.scalar(select(func.count()).select_from(Room)) == 5
        assert db.session.scalar(select(func.count()).select_from(PriceRule)) == 6
