"""Public JSON search endpoint.

Availability and pricing come from :mod:`app.booking` and :mod:`app.pricing`,
the same helpers the guest page uses, so the page and the API can never
disagree about what is free or what a night costs.
"""

from datetime import date, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from .booking import Stay, free_room_conditions, hotel_now, search_results, stay_error
from .extensions import db
from .models import Room


api = Blueprint("api", __name__, url_prefix="/api")


def parse_stay(values):
    try:
        stay = Stay(
            date.fromisoformat(values["check_in"]),
            date.fromisoformat(values["check_out"]),
            int(values["guests"]),
        )
    except (KeyError, TypeError, ValueError):
        return None, "Enter valid check-in and check-out dates and the number of guests."
    return stay, stay_error(stay, hotel_now())


def available_counts(session, stay, room_type_ids):
    """Count the rooms of each room type that are free for the whole stay."""

    if not room_type_ids:
        return {}
    rows = session.execute(
        db.select(Room.room_type_id, func.count())
        .where(Room.room_type_id.in_(room_type_ids), *free_room_conditions(stay))
        .group_by(Room.room_type_id)
    ).all()
    return {room_type_id: count for room_type_id, count in rows}


def nightly(rates):
    """Expand price segments into one entry per night, oldest first."""

    nights = []
    for rate in rates:
        day = rate.start
        while day < rate.end:
            nights.append({"date": day.isoformat(), "price": format(rate.amount, ".2f")})
            day += timedelta(days=1)
    return nights


@api.get("/search")
def search():
    stay, error = parse_stay(request.args)
    if error:
        return jsonify(code="INVALID_REQUEST", message=error), 400
    quotes = search_results(db.session, stay)
    counts = available_counts(db.session, stay, [item.room_type.id for item in quotes])
    return jsonify(
        check_in=stay.check_in.isoformat(),
        check_out=stay.check_out.isoformat(),
        guests=stay.guests,
        room_types=[
            {
                "id": item.room_type.id,
                "room_type": item.room_type.name,
                "description": item.room_type.description,
                "capacity": item.room_type.capacity,
                "base_price": format(item.room_type.base_price, ".2f"),
                "nights": nightly(item.rates),
                "total": format(item.total, ".2f"),
                "available_count": counts.get(item.room_type.id, 0),
            }
            for item in quotes
        ],
    )
