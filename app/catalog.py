from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy import exists, or_
from wtforms import DateField, Form, IntegerField
from wtforms.validators import InputRequired, NumberRange

from .extensions import db
from .models import PriceRule, Room, RoomBlock, RoomNight, RoomType


catalog = Blueprint("catalog", __name__)
HOTEL_ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Stay:
    check_in: date
    check_out: date
    guests: int


@dataclass(frozen=True)
class Rate:
    start: date
    end: date
    amount: Decimal
    rule_id: int | None


class SearchForm(Form):
    check_in = DateField("Check-in", validators=[InputRequired()])
    check_out = DateField("Check-out", validators=[InputRequired()])
    guests = IntegerField("Guests", validators=[InputRequired(), NumberRange(min=1)])


def hotel_now():
    clock = current_app.config.get("HOTEL_CLOCK")
    return clock().astimezone(HOTEL_ZONE) if clock else datetime.now(HOTEL_ZONE)


def stay_error(stay, now):
    today = now.date()
    if not today <= stay.check_in <= today + timedelta(days=14):
        return "Choose a check-in date from today through 14 days ahead."
    if stay.check_out <= stay.check_in:
        return "Check-out must be after check-in."
    if stay.check_in == today and now.time() >= time(22):
        return "Same-day bookings close at 22:00 Beijing time."
    if stay.guests < 1:
        return "Enter at least one guest."
    return None


def free_room_conditions(stay):
    booked = exists().where(
        RoomNight.room_id == Room.id,
        RoomNight.stay_date >= stay.check_in,
        RoomNight.stay_date < stay.check_out,
    )
    blocked = exists().where(
        RoomBlock.room_id == Room.id,
        RoomBlock.start_date < stay.check_out,
        RoomBlock.end_date > stay.check_in,
    )
    return (
        Room.operational_from <= stay.check_in,
        or_(Room.operational_until.is_(None), Room.operational_until >= stay.check_out),
        ~booked,
        ~blocked,
    )


def nightly_rates(session, room_type, stay):
    rules = session.scalars(
        db.select(PriceRule).where(
            PriceRule.room_type_id == room_type.id,
            PriceRule.start_date < stay.check_out,
            PriceRule.end_date > stay.check_in,
        )
    ).all()
    boundaries = {stay.check_in, stay.check_out}
    for rule in rules:
        boundaries.update((max(stay.check_in, rule.start_date), min(stay.check_out, rule.end_date)))
    days = sorted(boundaries)
    rates = []
    for start, end in zip(days, days[1:]):
        matches = [rule for rule in rules if rule.start_date <= start < rule.end_date]
        best = max(matches, key=lambda rule: (rule.kind == "date", rule.start_date, rule.id), default=None)
        amount = best.amount if best else room_type.base_price
        rule_id = best.id if best else None
        if rates and (rates[-1].amount, rates[-1].rule_id) == (amount, rule_id):
            rates[-1] = Rate(rates[-1].start, end, amount, rule_id)
        else:
            rates.append(Rate(start, end, amount, rule_id))
    return rates


def total_for_rates(rates):
    return sum((rate.amount * (rate.end - rate.start).days for rate in rates), Decimal("0.00"))


def room_type_payload(room_type):
    return {
        "id": room_type.id,
        "name": room_type.name,
        "description": room_type.description,
        "capacity": room_type.capacity,
        "base_price": format(room_type.base_price, ".2f"),
    }


def search_results(session, stay):
    whole_stay_room = exists().where(Room.room_type_id == RoomType.id, *free_room_conditions(stay))
    room_types = session.scalars(
        db.select(RoomType)
        .where(RoomType.listed.is_(True), RoomType.capacity >= stay.guests, whole_stay_room)
        .order_by(RoomType.capacity, RoomType.name, RoomType.id)
    ).all()
    results = []
    for room_type in room_types:
        rates = nightly_rates(session, room_type, stay)
        results.append({
            **room_type_payload(room_type),
            "total": format(total_for_rates(rates), ".2f"),
            "rates": [
                {
                    "start": rate.start.isoformat(),
                    "end": rate.end.isoformat(),
                    "nights": (rate.end - rate.start).days,
                    "amount": format(rate.amount, ".2f"),
                }
                for rate in rates
            ],
        })
    return results


def parse_search(form, now):
    if not form.validate():
        return None, "Enter valid check-in and check-out dates and the number of guests."
    stay = Stay(form.check_in.data, form.check_out.data, form.guests.data)
    return stay, stay_error(stay, now)


@catalog.get("/")
def index():
    now = hotel_now()
    searched = any(field in request.args for field in ("check_in", "check_out", "guests"))
    form = SearchForm(request.args if searched else None, guests=1)
    error = None
    if searched:
        stay, error = parse_search(form, now)
        results = [] if error else search_results(db.session, stay)
    else:
        room_types = db.session.scalars(
            db.select(RoomType).where(RoomType.listed.is_(True)).order_by(RoomType.capacity, RoomType.name)
        )
        results = [room_type_payload(room_type) for room_type in room_types]
    return render_template(
        "catalog/index.html",
        form=form,
        results=results,
        searched=searched,
        error=error,
        today=now.date(),
        last_arrival=now.date() + timedelta(days=14),
    ), 400 if error else 200


@catalog.get("/api/availability")
def availability():
    stay, error = parse_search(SearchForm(request.args), hotel_now())
    if error:
        return jsonify(code="INVALID_REQUEST", message=error), 400
    return jsonify(room_types=search_results(db.session, stay))
