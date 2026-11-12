from datetime import timedelta
from functools import wraps
from uuid import uuid4

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import DateField, IntegerField, StringField, SubmitField
from wtforms.validators import InputRequired, Length, NumberRange, Optional
from wtforms.widgets import HiddenInput

from .booking import (
    BookingRejected,
    Stay,
    create_booking,
    get_booking,
    hotel_now,
    listed_room_types,
    quote_token,
    recent_bookings,
    search_results,
    stay_error,
)
from .extensions import db
from .models import Booking, BookingRequest, Customer, GUEST, PaymentRecord


guest = Blueprint("guest", __name__, url_prefix="/guest")


class SearchForm(FlaskForm):
    check_in = DateField("Check-in", validators=[InputRequired()])
    check_out = DateField("Check-out", validators=[InputRequired()])
    guests = IntegerField("Guests", validators=[InputRequired(), NumberRange(min=1, max=2147483647)])
    submit = SubmitField("Search rooms")


class BookForm(FlaskForm):
    room_type_id = IntegerField(widget=HiddenInput(), validators=[InputRequired()])
    check_in = DateField(widget=HiddenInput(), validators=[InputRequired()])
    check_out = DateField(widget=HiddenInput(), validators=[InputRequired()])
    guests = IntegerField(
        widget=HiddenInput(), validators=[InputRequired(), NumberRange(min=1, max=2147483647)]
    )
    note = StringField("Note (optional)", validators=[Optional(), Length(max=100)])
    idempotency_key = StringField(widget=HiddenInput(), validators=[InputRequired(), Length(max=64)])
    quote = StringField(widget=HiddenInput(), validators=[InputRequired()])
    submit = SubmitField("Book this room")


def guest_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != GUEST:
            return redirect(url_for("auth.not_ready"))
        return view(*args, **kwargs)

    return wrapped


def parse_search(form, now):
    if not form.validate():
        return None, "Enter valid check-in and check-out dates and the number of guests."
    stay = Stay(form.check_in.data, form.check_out.data, form.guests.data)
    return stay, stay_error(stay, now)


@guest.get("/")
@guest_required
def index():
    now = hotel_now()
    searched = any(key in request.args for key in ("check_in", "check_out", "guests"))
    form = SearchForm(request.args if searched else None, meta={"csrf": False})
    if not searched:
        form.check_in.data = now.date() + timedelta(days=2)
        form.check_out.data = now.date() + timedelta(days=4)
        form.guests.data = 1

    error = None
    stay = None
    offers = []
    if searched:
        stay, error = parse_search(form, now)
        if not error:
            offers = [
                {
                    "room_type": quote.room_type, "total": quote.total, "rates": quote.rates,
                    "key": str(uuid4()), "quote": quote_token(quote.room_type.id, stay, quote.rates),
                }
                for quote in search_results(db.session, stay)
            ]
    else:
        offers = [
            {"room_type": room_type, "total": None, "rates": [], "key": None}
            for room_type in listed_room_types(db.session)
        ]

    return render_template(
        "guest/index.html",
        form=form,
        searched=searched,
        error=error,
        stay=stay,
        offers=offers,
        recent=recent_bookings(db.session, current_user.id, 3),
        today=now.date(),
        last_arrival=now.date() + timedelta(days=14),
    ), 400 if error else 200


@guest.post("/book")
@guest_required
def book():
    form = BookForm()
    if not form.validate_on_submit():
        flash("That booking form is no longer valid. Search again.", "error")
        return redirect(url_for("guest.index"))

    stay = Stay(form.check_in.data, form.check_out.data, form.guests.data)
    try:
        result = create_booking(
            current_user.id,
            form.room_type_id.data,
            stay,
            form.idempotency_key.data,
            note=form.note.data,
            confirmed_quote=form.quote.data,
        )
    except BookingRejected as exc:
        flash(str(exc), "error")
        return redirect(
            url_for(
                "guest.index",
                check_in=stay.check_in.isoformat(),
                check_out=stay.check_out.isoformat(),
                guests=stay.guests,
            )
        )

    if result.status == "confirmed":
        flash(f"Booking BK-{result.id:06d} is confirmed.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=result.id))


@guest.get("/bookings")
@guest_required
def bookings():
    own = db.session.scalars(
        db.select(Booking)
        .where(Booking.customer.has(Customer.user_id == current_user.id))
        .order_by(Booking.created_at.desc(), Booking.id.desc())
    ).all()
    return render_template("guest/bookings.html", bookings=own)


@guest.get("/bookings/<int:booking_id>")
@guest_required
def booking_detail(booking_id):
    booking = get_booking(db.session, booking_id, current_user.id)
    if booking is None:
        abort(404)
    payments = db.session.scalars(db.select(PaymentRecord).where(
        PaymentRecord.booking_id == booking.id, PaymentRecord.result == "success",
    ).order_by(PaymentRecord.id)).all()
    changes = db.session.scalars(db.select(BookingRequest).where(
        BookingRequest.booking_id == booking.id,
    ).order_by(BookingRequest.id.desc())).all()
    return render_template("guest/booking_detail.html", booking=booking, payments=payments, changes=changes)
