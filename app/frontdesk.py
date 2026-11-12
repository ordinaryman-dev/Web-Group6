"""The front desk: today's arrivals, in-house guests, departures, blocks and requests."""

from datetime import date, timedelta
from decimal import Decimal
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from wtforms import (
    DateField,
    DecimalField,
    HiddenField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TimeField,
)
from wtforms.validators import InputRequired, Length, NumberRange, Optional

from . import desk
from .booking import Stay, hotel_now
from .extensions import db
from .models import (
    MANAGER,
    Booking,
    BookingRequest,
    Customer,
    Room,
    RoomBlock,
    RoomType,
    STAFF,
)


frontdesk = Blueprint("frontdesk", __name__)

BOARD_DAYS = 4
MAX_BOARD_DAYS = 21


class PaymentForm(FlaskForm):
    amount = DecimalField("Amount", validators=[Optional(), NumberRange(min=Decimal("0.01"))])
    submit = SubmitField("Record payment")


class WalkInForm(FlaskForm):
    customer_id = HiddenField()
    name = StringField("Guest name", validators=[Optional(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=40)])
    room_type_id = SelectField("Room type", coerce=int, validators=[InputRequired()])
    check_in = DateField("Check-in", validators=[InputRequired()])
    check_out = DateField("Check-out", validators=[InputRequired()])
    guests = IntegerField("Guests", validators=[InputRequired(), NumberRange(min=1, max=6)])
    note = StringField("Note (optional)", validators=[Optional(), Length(max=100)])
    submit = SubmitField("Open the room and check in")


class BlockForm(FlaskForm):
    room_id = SelectField("Room", coerce=int, validators=[InputRequired()])
    start_date = DateField("From", validators=[InputRequired()])
    end_date = DateField("To", validators=[InputRequired()])
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Register block")


class ReassignForm(FlaskForm):
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Reassign room")


class DecisionForm(FlaskForm):
    comment = StringField("Decision note (optional)", validators=[Optional(), Length(max=240)])
    submit = SubmitField("Save")


class ContactForm(FlaskForm):
    result = SelectField(
        "Contact result",
        choices=[
            ("reached_late", "Reached - arriving later"),
            ("reached_not_coming", "Reached - not coming"),
            ("no_answer", "No answer"),
            ("wrong_number", "Wrong number"),
        ],
        validators=[InputRequired()],
    )
    new_expected = TimeField("New expected arrival", validators=[Optional()])
    note = StringField("Note (optional)", validators=[Optional(), Length(max=240)])
    submit = SubmitField("Save contact")


class NoShowForm(FlaskForm):
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Mark no-show")


def desk_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role not in (STAFF, MANAGER):
            abort(403, description="The front desk is for hotel staff.")
        return view(*args, **kwargs)

    return wrapped


def _run(action, success):
    """One transaction per action: a rule failure changes nothing."""
    try:
        with Session(db.engine) as session, session.begin():
            action(session)
    except desk.DeskError as exc:
        flash(str(exc), "error")
    except IntegrityError:
        flash("That change collides with another record. Nothing was saved.", "error")
    else:
        flash(success() if callable(success) else success, "success")


def _room_type_choices():
    rows = db.session.scalars(
        select(RoomType).where(RoomType.listed.is_(True)).order_by(RoomType.base_price)
    ).all()
    return [(row.id, f"{row.name} · ¥{row.base_price:,.2f}") for row in rows]


def _room_choices():
    rows = db.session.scalars(select(Room).order_by(Room.number)).all()
    return [(row.id, row.number) for row in rows]


def _booking_or_404(booking_id):
    booking = db.session.get(Booking, booking_id)
    if booking is None:
        abort(404)
    return booking


def _parse_date(value, fallback):
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


def _board_filters(now):
    start = _parse_date(request.args.get("frm"), now.date())
    days = request.args.get("days", type=int) or BOARD_DAYS
    days = max(1, min(days, MAX_BOARD_DAYS))
    room_type_id = request.args.get("type", type=int) or None
    room_query = (request.args.get("room") or "").strip() or None
    return start, days, room_type_id, room_query


def _search_bookings(query):
    """Find a booking by reference or guest name, so the desk can open it directly."""
    query = (query or "").strip()
    if not query:
        return []
    statement = select(Booking).order_by(Booking.id.desc()).limit(10)
    if query.upper().startswith("BK-") and query[3:].isdigit():
        return db.session.scalars(statement.where(Booking.id == int(query[3:]))).all()
    return db.session.scalars(
        statement.where(Booking.customer.has(Customer.name.ilike(f"%{query}%")))
    ).all()


def _selected_booking(arrivals, in_house, matches):
    wanted = request.args.get("booking", type=int)
    if wanted:
        booking = db.session.get(Booking, wanted)
        if booking is not None:
            return booking
    if len(matches) == 1:
        return matches[0]
    return arrivals[0] if arrivals else (in_house[0] if in_house else None)


@frontdesk.get("/frontdesk")
@desk_required
def index():
    now = hotel_now()
    today = now.date()
    arrivals, in_house, departures = desk.desk_today(db.session, now)

    matches = _search_bookings(request.args.get("q"))
    selected = _selected_booking(arrivals, in_house, matches)

    start, days, room_type_id, room_query = _board_filters(now)
    board = desk.room_board(db.session, start, days, room_type_id, room_query)
    board_end = start + timedelta(days=days)

    blocks = db.session.scalars(
        select(RoomBlock)
        .where(RoomBlock.effective.is_(True),
               RoomBlock.start_date < board_end,
               RoomBlock.end_date > start)
        .order_by(RoomBlock.start_date, RoomBlock.room_id)
    ).all()

    conflicts = []
    for block in blocks:
        affected = db.session.scalars(
            select(Booking).where(
                Booking.room_id == block.room_id,
                Booking.status.in_((desk.CONFIRMED, desk.CHECKED_IN)),
                Booking.check_in < block.end_date,
                Booking.check_out > block.start_date,
            )
        ).all()
        for booking in affected:
            conflicts.append({"block": block, "booking": booking})

    walk_customer = None
    if request.args.get("walk_customer", type=int):
        walk_customer = db.session.get(Customer, request.args.get("walk_customer", type=int))
    walk_matches = desk.find_customers(db.session, request.args.get("lookup")) if not walk_customer else []

    walk_form = WalkInForm(prefix="walk")
    walk_form.room_type_id.choices = _room_type_choices()
    if not walk_form.is_submitted():
        walk_form.check_in.data = today
        walk_form.check_out.data = today + timedelta(days=1)
        walk_form.guests.data = 1
    if walk_customer is not None:
        walk_form.customer_id.data = walk_customer.id

    block_form = BlockForm(prefix="block")
    block_form.room_id.choices = _room_choices()
    if not block_form.is_submitted():
        block_form.start_date.data = today
        block_form.end_date.data = today + timedelta(days=2)

    checks = desk.check_in_issues(db.session, selected, now) if selected else []
    paid = desk.paid_total(db.session, selected.id) if selected else Decimal("0.00")

    contact_due = False
    if selected is not None and selected.status == desk.CONFIRMED:
        contact_due = now.replace(tzinfo=None) > desk.contact_deadline(selected)

    return render_template(
        "frontdesk/index.html",
        now=now,
        today=today,
        start=start,
        board_days=days,
        days=[start + timedelta(days=offset) for offset in range(days)],
        room_type_id=room_type_id,
        room_query=room_query,
        query=(request.args.get("q") or "").strip(),
        matches=matches,
        arrivals=arrivals,
        in_house=in_house,
        departures=departures,
        board=board,
        blocks=blocks,
        conflicts=conflicts,
        pending=desk.pending_requests(db.session),
        selected=selected,
        checks=checks,
        paid=paid,
        due=(selected.total - paid) if selected else Decimal("0.00"),
        contact_due=contact_due,
        payment_form=PaymentForm(prefix="pay"),
        reassign_form=ReassignForm(prefix="re"),
        walk_form=walk_form,
        walk_customer=walk_customer,
        walk_matches=walk_matches,
        block_form=block_form,
        decision_form=DecisionForm(prefix="req"),
        contact_form=ContactForm(prefix="con"),
        no_show_form=NoShowForm(prefix="ns"),
        room_types=db.session.scalars(select(RoomType).order_by(RoomType.name)).all(),
    )


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/pay")
@desk_required
def pay(booking_id):
    booking = _booking_or_404(booking_id)
    form = PaymentForm(prefix="pay")
    amount = form.amount.data if form.validate_on_submit() else None

    def action(session):
        desk.record_payment(session, session.get(Booking, booking_id), current_user.id, amount)

    _run(action, f"Payment recorded for {booking.reference}.")
    return redirect(url_for("frontdesk.index", booking=booking_id))


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/check-in")
@desk_required
def check_in(booking_id):
    booking = _booking_or_404(booking_id)
    now = hotel_now()

    def action(session):
        desk.check_in(session, booking_id, current_user.id, now)

    _run(action, f"{booking.reference} checked in.")
    return redirect(url_for("frontdesk.index", booking=booking_id))


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/check-out")
@desk_required
def check_out(booking_id):
    booking = _booking_or_404(booking_id)
    now = hotel_now()
    outcome = {}

    def action(session):
        outcome.update(desk.check_out(session, booking_id, current_user.id, now))

    _run(action, f"{booking.reference} checked out.")
    if outcome.get("late"):
        flash("Recorded after 12:00, so this is a late check-out.", "info")
    if outcome.get("early"):
        flash("Departed early: the unused nights were released.", "info")
    return redirect(url_for("frontdesk.index", booking=booking_id))


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/contact")
@desk_required
def contact(booking_id):
    booking = _booking_or_404(booking_id)
    form = ContactForm(prefix="con")
    if not form.validate_on_submit():
        flash("Choose what happened when you contacted the guest.", "error")
        return redirect(url_for("frontdesk.index", booking=booking_id))

    def action(session):
        desk.contact_customer(session, booking_id, current_user.id, form.result.data,
                             form.note.data, form.new_expected.data)

    _run(action, "Contact attempt recorded.")
    return redirect(url_for("frontdesk.index", booking=booking_id))


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/no-show")
@desk_required
def no_show(booking_id):
    booking = _booking_or_404(booking_id)
    form = NoShowForm(prefix="ns")
    now = hotel_now()
    if not form.validate_on_submit():
        flash("Give a reason before marking a no-show.", "error")
        return redirect(url_for("frontdesk.index", booking=booking_id))

    def action(session):
        desk.mark_no_show(session, booking_id, current_user.id, form.reason.data, now)

    _run(action, f"{booking.reference} marked as a no-show and the room released.")
    return redirect(url_for("frontdesk.index"))


@frontdesk.post("/frontdesk/walk-in")
@desk_required
def walk_in():
    form = WalkInForm(prefix="walk")
    form.room_type_id.choices = _room_type_choices()
    if not form.validate_on_submit():
        flash("Check the walk-in: guest, room type, dates and guests are required.", "error")
        return redirect(url_for("frontdesk.index"))

    now = hotel_now()
    stay = Stay(form.check_in.data, form.check_out.data, form.guests.data)
    if stay.check_in != now.date():
        flash("A walk-in starts today.", "error")
        return redirect(url_for("frontdesk.index"))

    booking_id = {}

    def action(session):
        booking = desk.create_walkin(
            session, current_user.id, form.room_type_id.data, stay,
            customer_id=int(form.customer_id.data) if form.customer_id.data else None,
            name=form.name.data, phone=form.phone.data, note=form.note.data,
        )
        booking_id["id"] = booking.id

    _run(action, "Walk-in booked, paid and checked in.")
    return redirect(url_for("frontdesk.index", booking=booking_id.get("id")))


@frontdesk.post("/frontdesk/blocks")
@desk_required
def create_block():
    form = BlockForm(prefix="block")
    form.room_id.choices = _room_choices()
    if not form.validate_on_submit():
        flash("Check the block: room, dates and a reason are required.", "error")
        return redirect(url_for("frontdesk.index"))

    conflicts = []

    def action(session):
        _, found = desk.create_block(
            session, form.room_id.data, form.start_date.data, form.end_date.data,
            form.reason.data, current_user.id,
        )
        conflicts.extend(found)

    _run(action, "Maintenance block saved.")
    if conflicts:
        flash(f"{len(conflicts)} booking(s) now conflict and need reassignment.", "info")
    return redirect(url_for("frontdesk.index"))


@frontdesk.post("/frontdesk/blocks/<int:block_id>/cancel")
@desk_required
def cancel_block(block_id):
    block = db.session.get(RoomBlock, block_id)
    if block is None:
        abort(404)

    def action(session):
        desk.cancel_block(session, block_id, current_user.id)

    _run(action, f"Block on room {block.room.number} cancelled.")
    return redirect(url_for("frontdesk.index"))


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/reassign")
@desk_required
def reassign(booking_id):
    booking = _booking_or_404(booking_id)
    form = ReassignForm(prefix="re")
    if not form.validate_on_submit():
        flash("Give a reason for the reassignment.", "error")
        return redirect(url_for("frontdesk.index", booking=booking_id))

    moved = {}

    def action(session):
        updated = desk.reassign(session, booking_id, current_user.id, form.reason.data)
        moved["room"] = updated.room.number

    _run(action, lambda: f"{booking.reference} moved to room {moved.get('room', '')}.")
    return redirect(url_for("frontdesk.index", booking=booking_id))


@frontdesk.post("/frontdesk/requests/<int:request_id>/approve")
@desk_required
def approve_request(request_id):
    row = db.session.get(BookingRequest, request_id)
    if row is None:
        abort(404)
    form = DecisionForm(prefix="req")
    comment = form.comment.data if form.validate_on_submit() else None

    def action(session):
        desk.approve_request(session, request_id, current_user.id, comment)

    _run(action, "Request approved and applied.")
    return redirect(url_for("frontdesk.index", booking=row.booking_id))


@frontdesk.post("/frontdesk/requests/<int:request_id>/reject")
@desk_required
def reject_request(request_id):
    row = db.session.get(BookingRequest, request_id)
    if row is None:
        abort(404)
    form = DecisionForm(prefix="req")
    comment = form.comment.data if form.validate_on_submit() else None

    def action(session):
        desk.reject_request(session, request_id, current_user.id, comment)

    _run(action, "Request rejected; the booking is unchanged.")
    return redirect(url_for("frontdesk.index", booking=row.booking_id))
