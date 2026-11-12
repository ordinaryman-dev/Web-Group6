from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import PriceRule, RoomType
from app.pricing import NightPrice, PricingError, group_rates, quote, total


TODAY = date(2030, 6, 18)


def make_room_type(app, base_price="100.00", capacity=2):
    with app.app_context():
        room_type = RoomType(
            code=f"pricing-{uuid4().hex[:8]}",
            name="Pricing test room",
            description="Created by tests/test_pricing.py",
            capacity=capacity,
            base_price=Decimal(base_price),
            listed=True,
        )
        db.session.add(room_type)
        db.session.commit()
        return room_type.id


def make_rule(app, room_type_id, kind, start, end, amount, active=True):
    with app.app_context():
        rule = PriceRule(
            room_type_id=room_type_id,
            kind=kind,
            start_date=start,
            end_date=end,
            amount=Decimal(amount),
            active=active,
        )
        db.session.add(rule)
        db.session.commit()
        return rule.id


def test_quote_returns_one_night_per_night_and_falls_back_to_base(app):
    room_type_id = make_room_type(app)
    with app.app_context():
        prices = quote(db.session, room_type_id, date(2030, 6, 20), date(2030, 6, 23))
        assert [price.stay_date for price in prices] == [
            date(2030, 6, 20), date(2030, 6, 21), date(2030, 6, 22),
        ]
        assert all(isinstance(price, NightPrice) for price in prices)
        assert [price.amount for price in prices] == [Decimal("100.00")] * 3
        assert [price.rule_id for price in prices] == [None, None, None]
        assert total(prices) == Decimal("300.00")


def test_date_rule_beats_season_and_season_end_is_exclusive(app):
    room_type_id = make_room_type(app)
    season_id = make_rule(app, room_type_id, "season", date(2030, 6, 1), date(2030, 6, 30), "120.00")
    date_id = make_rule(app, room_type_id, "date", date(2030, 6, 21), date(2030, 6, 22), "200.00")
    with app.app_context():
        prices = quote(db.session, room_type_id, date(2030, 6, 20), date(2030, 6, 23))
        assert [price.amount for price in prices] == [Decimal("120.00"), Decimal("200.00"), Decimal("120.00")]
        assert [price.rule_id for price in prices] == [season_id, date_id, season_id]
        assert total(prices) == Decimal("440.00")

        after = quote(db.session, room_type_id, date(2030, 6, 29), date(2030, 7, 1))
        assert [price.rule_id for price in after] == [season_id, None]


def test_later_start_wins_between_two_seasons(app):
    room_type_id = make_room_type(app)
    season_id = make_rule(app, room_type_id, "season", date(2030, 6, 1), date(2030, 6, 30), "120.00")
    holiday_id = make_rule(app, room_type_id, "season", date(2030, 6, 21), date(2030, 6, 24), "180.00")
    with app.app_context():
        prices = quote(db.session, room_type_id, date(2030, 6, 20), date(2030, 6, 25))
        assert [price.amount for price in prices] == [
            Decimal("120.00"), Decimal("180.00"), Decimal("180.00"), Decimal("180.00"), Decimal("120.00"),
        ]
        assert [price.rule_id for price in prices] == [
            season_id, holiday_id, holiday_id, holiday_id, season_id,
        ]


def test_retired_rule_is_ignored(app):
    room_type_id = make_room_type(app)
    make_rule(app, room_type_id, "season", date(2030, 6, 1), date(2030, 6, 30), "120.00", active=False)
    with app.app_context():
        prices = quote(db.session, room_type_id, date(2030, 6, 20), date(2030, 6, 22))
        assert [price.amount for price in prices] == [Decimal("100.00")] * 2
        assert [price.rule_id for price in prices] == [None, None]


def test_group_rates_merges_consecutive_equal_prices(app):
    room_type_id = make_room_type(app)
    season_id = make_rule(app, room_type_id, "season", date(2030, 6, 20), date(2030, 6, 22), "120.00")
    with app.app_context():
        prices = quote(db.session, room_type_id, date(2030, 6, 20), date(2030, 6, 24))
        rates = group_rates(prices)
        assert [(rate.start, rate.end, rate.amount, rate.rule_id) for rate in rates] == [
            (date(2030, 6, 20), date(2030, 6, 22), Decimal("120.00"), season_id),
            (date(2030, 6, 22), date(2030, 6, 24), Decimal("100.00"), None),
        ]


def test_quote_rejects_empty_stay_and_unknown_room_type(app):
    room_type_id = make_room_type(app)
    with app.app_context():
        with pytest.raises(PricingError) as empty:
            quote(db.session, room_type_id, date(2030, 6, 20), date(2030, 6, 20))
        assert empty.value.code == "INVALID_REQUEST"
        with pytest.raises(PricingError) as unknown:
            quote(db.session, 10_000_000, date(2030, 6, 20), date(2030, 6, 22))
        assert unknown.value.code == "NOT_FOUND"


def test_quote_leaves_the_caller_transaction_alone(app):
    room_type_id = make_room_type(app)
    with app.app_context():
        quote(db.session, room_type_id, date(2030, 6, 20), date(2030, 6, 22))
        assert not db.session.new and not db.session.dirty and not db.session.deleted
        assert db.session.scalar(select(PriceRule.id).where(PriceRule.room_type_id == room_type_id)) is None
