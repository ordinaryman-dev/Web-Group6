"""The front desk: today's arrivals, in-house guests, departures and blocks."""

from datetime import timedelta
from decimal import Decimal
from functools import wraps
from uuid import uuid4

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from wtforms import DateField, DecimalField, IntegerField, SelectField, StringField, SubmitField
from wtforms.validators import InputRequired, Length, NumberRange, Optional

from . import desk
from .audit import record_audit
from .booking import Stay, hotel_now
from .extensions import db
from .models import (
    MANAGER,
    Booking,
    BookingRequest,
    Room,
    RoomBlock,
    RoomNight,
    RoomType,
    STAFF,
)


frontdesk = Blueprint("frontdesk", __name__)

BOARD_DAYS = 4


class PaymentForm(FlaskForm):
    amount = DecimalField("Amount", validators=[Optional(), NumberRange(min=Decimal("0.01"))])
    submit = SubmitField("Record payment")


class WalkInForm(FlaskForm):
    name = StringField("Guest name", validators=[InputRequired(), Length(max=120)])
    phone = StringField("Phone", validators=[InputRequired(), Length(max=40)])
    room_type_id = SelectField("Room type", coerce=int, validators=[InputRequired()])
    check_in = DateField("Check-in", validators=[InputRequired()])
    check_out = DateField("Check-out", validators=[InputRequired()])
    guests = IntegerField("Guests", validators=[InputRequired(), NumberRange(min=1, max=6)])
    note = StringField("Note (optional)", validators=[Optional(), Length(max=100)])
    submit = SubmitField("Open the room")


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
    comment = StringField("Note (optional)", validators=[Optional(), Length(max=240)])
    submit = SubmitField("Save")


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


def _selected_booking(arrivals, in_house):
    wanted = request.args.get("booking", type=int)
    if wanted:
        booking = db.session.get(Booking, wanted)
        if booking is not None:
            return booking
    return arrivals[0] if arrivals else (in_house[0] if in_house else None)


@frontdesk.get("/frontdesk")
@desk_required
def index():
    now = hotel_now()
    today = now.date()
    arrivals, in_house, departures = desk.desk_today(db.session, now)
    selected = _selected_booking(arrivals, in_house)

    blocks = db.session.scalars(
        select(RoomBlock)
        .where(RoomBlock.effective.is_(True),
               RoomBlock.start_date < today + timedelta(days=BOARD_DAYS),
               RoomBlock.end_date > today)
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

    walk_form = WalkInForm(prefix="walk")
    walk_form.room_type_id.choices = _room_type_choices()
    if not walk_form.is_submitted():
        walk_form.check_in.data = today
        walk_form.check_out.data = today + timedelta(days=1)
        walk_form.guests.data = 1

    block_form = BlockForm(prefix="block")
    block_form.room_id.choices = _room_choices()
    if not block_form.is_submitted():
        block_form.start_date.data = today
        block_form.end_date.data = today + timedelta(days=2)

    checks = desk.check_in_issues(db.session, selected, now) if selected else []
    paid = desk.paid_total(db.session, selected.id) if selected else Decimal("0.00")

    return render_template(
        "frontdesk/index.html",
        now=now,
        today=today,
        days=[today + timedelta(days=offset) for offset in range(BOARD_DAYS)],
        arrivals=arrivals,
        in_house=in_house,
        departures=departures,
        board=desk.room_board(db.session, today, BOARD_DAYS),
        blocks=blocks,
        conflicts=conflicts,
        pending=desk.pending_requests(db.session),
        selected=selected,
        checks=checks,
        paid=paid,
        due=(selected.total - paid) if selected else Decimal("0.00"),
        payment_form=PaymentForm(prefix="pay"),
        reassign_form=ReassignForm(prefix="re"),
        walk_form=walk_form,
        block_form=block_form,
        decision_form=DecisionForm(prefix="req"),
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

    def action(session):
        desk.check_out(session, booking_id, current_user.id, now)

    _run(action, f"{booking.reference} checked out.")
    return redirect(url_for("frontdesk.index", booking=booking_id))


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/no-show")
@desk_required
def no_show(booking_id):
    booking = _booking_or_404(booking_id)
    now = hotel_now()

    def action(session):
        target = session.get(Booking, booking_id)
        if target.status != desk.CONFIRMED:
            raise desk.DeskError("Only a confirmed booking can be marked as a no-show.")
        if target.check_in >= now.date():
            raise desk.DeskError("A no-show can only be recorded after the arrival date.")
        session.execute(db.delete(RoomNight).where(RoomNight.booking_id == target.id))
        target.status = "no_show"
        record_audit(session, current_user.id, "mark_no_show", "booking", target.id)

    _run(action, f"{booking.reference} marked as a no-show.")
    return redirect(url_for("frontdesk.index"))


@frontdesk.post("/frontdesk/walk-in")
@desk_required
def walk_in():
    form = WalkInForm(prefix="walk")
    form.room_type_id.choices = _room_type_choices()
    if not form.validate_on_submit():
        flash("Check the walk-in: name, phone, room type and dates are required.", "error")
        return redirect(url_for("frontdesk.index"))

    now = hotel_now()
    stay = Stay(form.check_in.data, form.check_out.data, form.guests.data)
    if stay.check_in != now.date():
        flash("A walk-in starts today.", "error")
        return redirect(url_for("frontdesk.index"))

    booking_id = None

    def action(session):
        nonlocal booking_id
        booking = desk.create_walkin(
            session, current_user.id, form.name.data, form.phone.data,
            form.room_type_id.data, stay, form.note.data,
        )
        booking_id = booking.id

    _run(action, "Walk-in opened.")
    return redirect(url_for("frontdesk.index", booking=booking_id) if booking_id
                    else url_for("frontdesk.index"))


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
