"""The front desk: a floor-by-floor room board, orders, tasks and a booking drawer.

The page is server-rendered. Every action still runs through :mod:`app.desk`, so the
rules shown as disabled buttons in the browser are enforced again on the server.
"""

import re
from datetime import date, timedelta, timezone
from decimal import Decimal
from functools import wraps
from urllib.parse import parse_qsl, urlsplit

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import or_, select
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
from wtforms.widgets import HiddenInput

from . import desk
from .booking import (
    BookingRejected,
    HOTEL_ZONE,
    Stay,
    cancel_confirmed_booking,
    cancellation_deadline,
    hotel_now,
)
from .extensions import db
from .models import (
    MANAGER,
    Booking,
    BookingRequest,
    Customer,
    PaymentRecord,
    Room,
    RoomBlock,
    RoomNight,
    RoomType,
    STAFF,
    User,
)


frontdesk = Blueprint("frontdesk", __name__)

TIMELINE_DAYS = 15  # today plus fourteen days
MAX_STAY_DAYS = 21
STATE_LABELS = {
    "free": "Free",
    "arriving": "Arriving",
    "reserved": "Reserved",
    "in_house": "In house",
    "departing": "Departing",
    "dirty": "Dirty",
    "blocked": "Out of service",
}
BED_LABELS = {
    "single": "Single",
    "double": "Double",
    "twin": "Twin",
    "king": "King",
    "suite": "Suite",
}
ORDER_TABS = {
    "arrivals": "Arrivals",
    "inhouse": "In house",
    "departures": "Departures",
    "all": "All orders",
}


class PaymentForm(FlaskForm):
    amount = DecimalField("Amount", validators=[Optional(), NumberRange(min=Decimal("0.01"))])
    submit = SubmitField("Record payment")


class RefundForm(FlaskForm):
    amount = DecimalField("Refund", validators=[Optional(), NumberRange(min=Decimal("0.01"))])
    submit = SubmitField("Record refund")


class WalkInForm(FlaskForm):
    customer_id = HiddenField()
    room_id = IntegerField(widget=HiddenInput())
    name = StringField("Guest name", validators=[Optional(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=40)])
    room_type_id = SelectField("Room type", coerce=int, validators=[InputRequired()])
    check_in = DateField("Check-in", validators=[InputRequired()])
    check_out = DateField("Check-out", validators=[InputRequired()])
    guests = IntegerField("Guests", validators=[InputRequired(), NumberRange(min=1, max=6)])
    note = StringField("Note (optional)", validators=[Optional(), Length(max=100)])
    submit = SubmitField("Create order and check in")


class BlockForm(FlaskForm):
    room_id = IntegerField("Room", validators=[InputRequired()], widget=HiddenInput())
    start_date = DateField("From", validators=[InputRequired()])
    end_date = DateField("To", validators=[InputRequired()])
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Set out of service")


class ReassignForm(FlaskForm):
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Move to a free room")


class DecisionForm(FlaskForm):
    comment = StringField("Decision note", validators=[Optional(), Length(max=240)])
    submit = SubmitField("Save")


class ContactForm(FlaskForm):
    result = SelectField(
        "Contact result",
        choices=[
            ("", "Choose a result"),
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
    submit = SubmitField("Mark no-show and release")


class CancelBookingForm(FlaskForm):
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Cancel with manager authority")


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


def _back(**overrides):
    """Return to the desk exactly where the form was opened, never to another host."""
    candidate = request.form.get("back") or ""
    parsed = urlsplit(candidate)
    args = dict(parse_qsl(parsed.query)) if parsed.path in ("/frontdesk", "/frontdesk/") else {}
    for key, value in overrides.items():
        if value is None:
            args.pop(key, None)
        else:
            args[key] = value
    return redirect(url_for("frontdesk.index", **args))


def _room_type_choices():
    rows = db.session.scalars(
        select(RoomType).where(RoomType.listed.is_(True)).order_by(RoomType.base_price)
    ).all()
    return [(row.id, f"{row.name} · ¥{row.base_price:,.2f}") for row in rows]


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


def _clamp(value, low, high):
    return max(low, min(value, high))


def _booking_filter(query, statement=None):
    if statement is None:
        statement = select(Booking)
    query = (query or "").strip()
    if not query:
        return statement
    if query.upper().startswith("BK-") and query[3:].isdigit():
        return statement.where(Booking.id == int(query[3:]))
    like = f"%{query}%"
    return statement.where(or_(
        Booking.customer.has(Customer.name.ilike(like)),
        Booking.customer.has(Customer.phone.ilike(like)),
        Booking.room.has(Room.number.ilike(like)),
    ))


def _search_bookings(query):
    """Find a booking by reference, guest name, phone or room number."""
    if not (query or "").strip():
        return []
    statement = _booking_filter(query, select(Booking)).order_by(Booking.id.desc()).limit(10)
    return db.session.scalars(statement).all()


def _floor_key(number):
    match = re.fullmatch(r"(\d+)(\d{2})", number or "")
    if match:
        return match.group(1)
    match = re.match(r"([A-Za-z]+)", number or "")
    return match.group(1).upper() if match else "OTHER"


def _floor_label(key):
    return f"{int(key)}F" if key.isdigit() else key


def _floor_sort(key):
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def _mask_name(name):
    """Keep the board readable without printing a full guest name in public."""
    name = (name or "").strip()
    if not name:
        return "Guest"
    if re.search(r"[\u4e00-\u9fff]", name):
        return name[0] + "**"
    parts = name.split()
    if len(parts) == 1:
        return parts[0][0] + "***"
    # Pinyin names in the demo put the surname first; Western names put it last.
    # Either way the first token plus the other initial is enough at the desk.
    return f"{parts[0]} {parts[-1][0]}."


def _submitted_local(row):
    if row.submitted_at is None:
        return None
    return (
        row.submitted_at.replace(tzinfo=timezone.utc)
        .astimezone(HOTEL_ZONE)
        .replace(tzinfo=None)
    )


def _payment_rows(session, booking):
    return session.execute(
        select(PaymentRecord, User.email)
        .join(User, User.id == PaymentRecord.actor_user_id)
        .where(PaymentRecord.booking_id == booking.id)
        .order_by(PaymentRecord.id.desc())
    ).all()


def _request_target(session, row):
    target = row.target or {}
    if row.kind == "dates":
        return f"{target.get('check_in', '?')} → {target.get('check_out', '?')}"
    if row.kind == "extend":
        return f"Until {target.get('check_out', '?')}"
    if row.kind == "room_type":
        room_type = session.get(RoomType, target.get("room_type_id"))
        return room_type.name if room_type else "Another room type"
    if row.kind == "cancel":
        return target.get("reason") or "Cancel the booking"
    return "—"


def _task_rows(session, pending, now):
    rows = []
    for row in pending:
        booking = row.booking
        submitted = _submitted_local(row)
        late = (
            row.kind == "cancel"
            and submitted is not None
            and submitted > cancellation_deadline(booking)
        )
        rows.append({
            "request": row,
            "booking": booking,
            "target": _request_target(session, row),
            "submitted": submitted,
            "late_cancel": late,
            "manager_required": late,
            "can_approve": (not late) or current_user.role == MANAGER,
        })
    return rows


def _order_rows(session, tab, status, query, now):
    today = now.date()
    statement = select(Booking)
    statement = _booking_filter(query, statement)
    if tab == "arrivals":
        statement = statement.where(
            Booking.check_in == today,
            Booking.status == desk.CONFIRMED,
            Booking.checked_in_at.is_(None),
        ).order_by(Booking.expected_arrival.is_(None), Booking.expected_arrival, Booking.id)
    elif tab == "inhouse":
        statement = statement.where(Booking.status == desk.CHECKED_IN).order_by(Booking.room_id)
    elif tab == "departures":
        statement = statement.where(
            Booking.status == desk.CHECKED_IN, Booking.check_out <= today,
        ).order_by(Booking.check_out, Booking.id)
    else:
        if status:
            statement = statement.where(Booking.status == status)
        statement = statement.order_by(Booking.check_in.desc(), Booking.id.desc())
    bookings = session.scalars(statement.limit(120)).all()
    paid = desk.paid_totals(session, [booking.id for booking in bookings])
    rows = []
    for booking in bookings:
        deadline = desk.contact_deadline(booking)
        late = (
            booking.status == desk.CONFIRMED
            and booking.check_in <= today
            and now.replace(tzinfo=None) > deadline
        )
        rows.append({
            "booking": booking,
            "paid": paid.get(booking.id, Decimal("0.00")),
            "due": booking.total - paid.get(booking.id, Decimal("0.00")),
            "late": late,
            "overdue": booking.status == desk.CHECKED_IN and booking.check_out < today,
        })
    return rows


@frontdesk.get("/frontdesk")
@desk_required
def index():
    now = hotel_now()
    today = now.date()
    is_manager = current_user.role == MANAGER

    view = request.args.get("view") or "grid"
    if view not in ("grid", "orders", "tasks"):
        view = "grid"

    query = (request.args.get("q") or "").strip()
    matches = _search_bookings(query)

    booking_param = (
        db.session.get(Booking, request.args.get("booking", type=int))
        if request.args.get("booking", type=int) else None
    )
    room_param = (
        db.session.get(Room, request.args.get("room", type=int))
        if request.args.get("room", type=int) else None
    )
    if room_param is None and booking_param is not None:
        room_param = booking_param.room

    date_param = request.args.get("date")
    if date_param:
        selected_date = _parse_date(date_param, today)
    elif booking_param is not None and booking_param.status == desk.CONFIRMED:
        selected_date = booking_param.check_in
    else:
        selected_date = today
    selected_date = _clamp(selected_date, today, today + timedelta(days=14))

    state_filter = request.args.get("state") or "all"
    if state_filter not in STATE_LABELS:
        state_filter = "all"
    bed_filter = (request.args.get("bed") or "").strip()
    if bed_filter not in BED_LABELS:
        bed_filter = ""
    window_filter = (request.args.get("window") or "").strip()
    if window_filter not in ("with", "without"):
        window_filter = ""
    type_filter = request.args.get("type", type=int)
    stay_on = request.args.get("stay") == "1"
    stay_end = _parse_date(request.args.get("check_out"), selected_date + timedelta(days=1))
    stay_end = _clamp(
        stay_end,
        selected_date + timedelta(days=1),
        today + timedelta(days=MAX_STAY_DAYS),
    )

    # One board covers the timeline and any whole-stay filter; the grid itself renders
    # only the active floor, so 300 rooms never turn into 300 rows of HTML.
    wide_days = max(TIMELINE_DAYS, (stay_end - today).days + 2)
    wide_days = _clamp(wide_days, 1, MAX_STAY_DAYS + 1)
    board = desk.room_board(db.session, today, wide_days)
    board_by_room = {
        row["room"].id: {cell["date"]: cell for cell in row["cells"]} for row in board
    }
    rooms = [row["room"] for row in board]

    room_types = db.session.scalars(select(RoomType).order_by(RoomType.name)).all()
    room_type_by_id = {room_type.id: room_type for room_type in room_types}

    occupied_ids = set(db.session.scalars(
        select(RoomNight.room_id)
        .where(RoomNight.stay_date >= selected_date, RoomNight.stay_date < stay_end)
        .distinct()
    ).all())
    blocked_ids = set(db.session.scalars(
        select(RoomBlock.room_id)
        .where(
            RoomBlock.effective.is_(True),
            RoomBlock.start_date < stay_end,
            RoomBlock.end_date > selected_date,
        )
        .distinct()
    ).all())
    checked_in_ids = set(db.session.scalars(
        select(Booking.room_id).where(Booking.status == desk.CHECKED_IN).distinct()
    ).all())
    overdue_ids = set(db.session.scalars(
        select(Booking.room_id)
        .where(Booking.status == desk.CHECKED_IN, Booking.check_out < selected_date)
        .distinct()
    ).all())

    room_info = []
    for room in rooms:
        cells = board_by_room[room.id]
        cell = cells.get(selected_date) or {
            "date": selected_date, "state": "free", "booking": None, "block": None,
        }
        room_info.append({
            "room": room,
            "room_type": room_type_by_id[room.room_type_id],
            "cell": cell,
            "floor": _floor_key(room.number),
            "stay_free": (
                room.id not in occupied_ids
                and room.id not in blocked_ids
                and room.id not in overdue_ids
                and not (selected_date == today and room.id in checked_in_ids)
                and not (selected_date == today and room.housekeeping == "dirty")
            ),
        })

    floor_counts = {}
    for info in room_info:
        floor = info["floor"]
        counts = floor_counts.setdefault(floor, {"total": 0, "active": 0, "free": 0})
        counts["total"] += 1
        if info["cell"]["state"] == "free":
            counts["free"] += 1
        else:
            counts["active"] += 1

    requested_floor = (request.args.get("floor") or "").strip()
    if requested_floor in floor_counts:
        active_floor = requested_floor
    elif room_param is not None and _floor_key(room_param.number) in floor_counts:
        active_floor = _floor_key(room_param.number)
    elif floor_counts:
        active_floor = sorted(
            floor_counts,
            key=lambda key: (-floor_counts[key]["active"], _floor_sort(key)),
        )[0]
    else:
        active_floor = ""

    spotlight = (
        state_filter != "all" or bool(bed_filter) or bool(window_filter)
        or bool(type_filter) or stay_on
    )
    cards = []
    state_counts = {}
    for info in room_info:
        if info["floor"] != active_floor:
            continue
        info["dim"] = (
            (state_filter != "all" and info["cell"]["state"] != state_filter)
            or (bool(bed_filter) and info["room_type"].bed_type != bed_filter)
            or (window_filter == "with" and not info["room"].has_window)
            or (window_filter == "without" and info["room"].has_window)
            or (type_filter is not None and info["room"].room_type_id != type_filter)
            or (stay_on and not info["stay_free"])
        )
        info["headline"] = _mask_name(
            info["cell"]["booking"].customer.name if info["cell"]["booking"] else ""
        )
        state_counts[info["cell"]["state"]] = state_counts.get(info["cell"]["state"], 0) + 1
        cards.append(info)
    for info in room_info:
        if info["floor"] != active_floor:
            info["dim"] = False

    timeline = []
    for offset in range(TIMELINE_DAYS):
        day = today + timedelta(days=offset)
        counts = {}
        for row in board:
            state = row["cells"][offset]["state"]
            counts[state] = counts.get(state, 0) + 1
        timeline.append({
            "date": day,
            "free": counts.get("free", 0),
            "arrivals": counts.get("arriving", 0),
            "in_house": counts.get("in_house", 0),
        })

    floor_tabs = [
        {
            "key": key,
            "label": _floor_label(key),
            "total": floor_counts[key]["total"],
            "active": floor_counts[key]["active"],
            "free": floor_counts[key]["free"],
        }
        for key in sorted(floor_counts, key=_floor_sort)
    ]

    arrivals_today, in_house, departures_today = desk.desk_today(db.session, now)
    pending = desk.pending_requests(db.session)
    late_candidates = db.session.scalars(
        select(Booking)
        .where(
            Booking.status == desk.CONFIRMED,
            Booking.checked_in_at.is_(None),
            Booking.check_in <= today,
        )
        .order_by(Booking.check_in, Booking.id)
    ).all()
    late_arrivals = []
    for booking in late_candidates:
        deadline = desk.contact_deadline(booking)
        if now.replace(tzinfo=None) > deadline:
            late_arrivals.append({"booking": booking, "deadline": deadline})

    occupied_today = len(set(db.session.scalars(
        select(RoomNight.room_id).where(RoomNight.stay_date == today).distinct()
    ).all()))
    dirty_rooms = db.session.scalars(
        select(Room).where(Room.housekeeping == "dirty").order_by(Room.number)
    ).all()

    # Maintenance conflicts: a block never overwrites a booking, it flags the pair.
    blocks = db.session.scalars(
        select(RoomBlock)
        .where(
            RoomBlock.effective.is_(True),
            RoomBlock.start_date < today + timedelta(days=TIMELINE_DAYS),
            RoomBlock.end_date > today,
        )
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

    notifications = []
    if pending:
        notifications.append({
            "kind": "requests",
            "tone": "amber",
            "title": f"{len(pending)} pending request{'s' if len(pending) != 1 else ''}",
            "text": "Guest changes waiting for a decision.",
        })
    if late_arrivals:
        notifications.append({
            "kind": "late",
            "tone": "blue",
            "title": f"{len(late_arrivals)} late arrival{'s' if len(late_arrivals) != 1 else ''}",
            "text": "Expected time has passed; contact the guest.",
        })
    if conflicts:
        notifications.append({
            "kind": "conflict",
            "tone": "red",
            "title": f"{len(conflicts)} maintenance conflict{'s' if len(conflicts) != 1 else ''}",
            "text": "A block overlaps an active booking.",
        })
    if dirty_rooms:
        notifications.append({
            "kind": "dirty",
            "tone": "gray",
            "title": f"{len(dirty_rooms)} room{'s' if len(dirty_rooms) != 1 else ''} to clean",
            "text": "Mark them clean before selling them tonight.",
        })
    notification_total = len(pending) + len(late_arrivals) + len(conflicts) + len(dirty_rooms)

    # The drawer: an occupied cell opens its booking, a blocked cell opens the block,
    # a free cell opens walk-in / out-of-service actions.
    selected_booking = booking_param
    selected_room = room_param
    selected_cell = None
    if selected_room is not None:
        selected_cell = board_by_room.get(selected_room.id, {}).get(selected_date)
    if selected_booking is None and selected_room is not None and selected_cell is not None:
        # A blocked cell stays in room mode so the block and its conflicting booking
        # can be handled together; a plain occupied cell opens its booking.
        if selected_cell["block"] is None and selected_cell["booking"] is not None:
            selected_booking = selected_cell["booking"]

    drawer_mode = "booking" if selected_booking is not None else ("room" if selected_room else None)
    checks = []
    paid = Decimal("0.00")
    due = Decimal("0.00")
    payments = []
    check_in_allowed = False
    check_in_reason = None
    contact_due = False
    no_show_allowed = False
    no_show_hint = None
    deadline = None
    late_cancel = False
    checkout_late = False
    overdue_checkout = False
    early_checkout = False
    reassign_allowed = False
    can_pay = False
    can_refund = False
    can_cancel_directly = False
    booking_requests = []
    block_conflicts = []
    current_block = None

    if selected_booking is not None:
        paid = desk.paid_total(db.session, selected_booking.id)
        due = selected_booking.total - paid
        payments = _payment_rows(db.session, selected_booking)
        if selected_booking.status == desk.CONFIRMED:
            checks = desk.check_in_issues(db.session, selected_booking, now)
            check_in_allowed = (
                selected_booking.check_in == today and all(ok for _, ok, _ in checks)
            )
            failed = [detail for _, ok, detail in checks if not ok]
            check_in_reason = failed[0] if failed else None
            contact_due = now.replace(tzinfo=None) > desk.contact_deadline(selected_booking)
            no_show_allowed = (
                desk.no_show_ready(selected_booking, now)
                and selected_booking.last_contact_at is not None
            )
            if selected_booking.last_contact_at is None:
                no_show_hint = "Record a contact attempt before releasing the room."
            elif not desk.no_show_ready(selected_booking, now):
                no_show_hint = "The expected arrival time has not passed yet."
            deadline = cancellation_deadline(selected_booking)
            late_cancel = now.replace(tzinfo=None) > deadline
            reassign_allowed = selected_booking.check_in >= today
            can_cancel_directly = is_manager
        elif selected_booking.status == desk.CHECKED_IN:
            overdue_checkout = selected_booking.check_out < today
            checkout_late = (
                selected_booking.check_out == today and now.time() > desk.CHECK_OUT_BY
            )
            early_checkout = selected_booking.check_out > today
        can_pay = (
            selected_booking.status in (desk.CONFIRMED, desk.CHECKED_IN) and due > 0
        )
        can_refund = (
            selected_booking.status in (desk.CANCELLED, desk.CHECKED_OUT) and paid > 0
        )
        booking_requests = db.session.scalars(
            select(BookingRequest)
            .where(BookingRequest.booking_id == selected_booking.id)
            .order_by(BookingRequest.id.desc())
            .limit(6)
        ).all()
        if selected_cell is not None:
            current_block = selected_cell["block"]
    elif selected_room is not None and selected_cell is not None:
        current_block = selected_cell["block"]

    if current_block is not None:
        block_conflicts = db.session.scalars(
            select(Booking).where(
                Booking.room_id == current_block.room_id,
                Booking.status == desk.CONFIRMED,
                Booking.check_in < current_block.end_date,
                Booking.check_out > current_block.start_date,
            )
        ).all()

    walk_form = WalkInForm(prefix="walk")
    walk_form.room_type_id.choices = _room_type_choices()
    if selected_room is not None:
        # A walk-in opened from a room card is tied to that room, so the type follows
        # the room rather than offering a type that the room cannot satisfy.
        chosen_type = room_type_by_id.get(selected_room.room_type_id)
        if chosen_type is not None:
            walk_form.room_type_id.choices = [(
                chosen_type.id,
                f"{chosen_type.name} · ¥{chosen_type.base_price:,.2f}",
            )]
    if not walk_form.is_submitted():
        walk_form.check_in.data = today
        walk_form.check_out.data = today + timedelta(days=1)
        walk_form.guests.data = 1
    walk_customer = None
    if request.args.get("walk_customer", type=int):
        walk_customer = db.session.get(Customer, request.args.get("walk_customer", type=int))
    walk_matches = (
        desk.find_customers(db.session, request.args.get("lookup"))
        if not walk_customer else []
    )
    if walk_customer is not None:
        walk_form.customer_id.data = walk_customer.id
    if selected_room is not None:
        walk_form.room_id.data = selected_room.id
        walk_form.room_type_id.data = selected_room.room_type_id

    block_form = BlockForm(prefix="block")
    if not block_form.is_submitted():
        block_form.start_date.data = selected_date
        block_form.end_date.data = selected_date + timedelta(days=1)
    if selected_room is not None:
        block_form.room_id.data = selected_room.id

    walk_allowed = bool(
        selected_room is not None
        and drawer_mode == "room"
        and selected_date == today
        and selected_cell is not None
        and selected_cell["state"] == "free"
        and selected_room.housekeeping == "clean"
    )
    walk_reason = None
    if selected_room is not None:
        if selected_date != today:
            walk_reason = "A walk-in can only start today. Switch the timeline back to today."
        elif selected_cell is not None and selected_cell["state"] != "free":
            walk_reason = "This room is not free tonight."
        elif selected_room.housekeeping != "clean":
            walk_reason = "This room is still marked dirty. Mark it cleaned first."

    def _desk_url(**overrides):
        args = {key: value for key, value in request.args.items()}
        for key, value in overrides.items():
            if value is None:
                args.pop(key, None)
            else:
                args[key] = value
        return url_for("frontdesk.index", **args)

    close_url = _desk_url(room=None, booking=None)
    order_tab = request.args.get("tab") or "arrivals"
    if order_tab not in ORDER_TABS:
        order_tab = "arrivals"
    order_rows = (
        _order_rows(db.session, order_tab, request.args.get("status") or None, query, now)
        if view == "orders" else []
    )
    task_rows = _task_rows(db.session, pending, now) if view == "tasks" else []

    return render_template(
        "frontdesk/index.html",
        now=now,
        epoch_ms=int(now.timestamp() * 1000),
        today=today,
        view=view,
        query=query,
        matches=matches,
        selected_date=selected_date,
        timeline=timeline,
        floor_tabs=floor_tabs,
        active_floor=active_floor,
        floor_numbers=sum(1 for key in floor_counts if key.isdigit()),
        cards=cards,
        state_filter=state_filter,
        state_counts=state_counts,
        bed_filter=bed_filter,
        window_filter=window_filter,
        type_filter=type_filter,
        stay_on=stay_on,
        stay_end=stay_end,
        stay_min=selected_date + timedelta(days=1),
        stay_max=today + timedelta(days=MAX_STAY_DAYS),
        spotlight=spotlight,
        floor_counts=floor_counts,
        room_types=room_types,
        bed_labels=BED_LABELS,
        state_labels=STATE_LABELS,
        arrivals_today=arrivals_today,
        in_house=in_house,
        departures_today=departures_today,
        pending=pending,
        late_arrivals=late_arrivals,
        dirty_rooms=dirty_rooms,
        conflicts=conflicts,
        notifications=notifications,
        notification_total=notification_total,
        occupied_today=occupied_today,
        total_rooms=len(rooms),
        selected=selected_booking,
        selected_room=selected_room,
        selected_room_type=(
            room_type_by_id.get(selected_room.room_type_id) if selected_room else None
        ),
        selected_cell=selected_cell,
        drawer_mode=drawer_mode,
        close_url=close_url,
        checks=checks,
        paid=paid,
        due=due,
        payments=payments,
        check_in_allowed=check_in_allowed,
        check_in_reason=check_in_reason,
        contact_due=contact_due,
        no_show_allowed=no_show_allowed,
        no_show_hint=no_show_hint,
        deadline=deadline,
        late_cancel=late_cancel,
        checkout_late=checkout_late,
        overdue_checkout=overdue_checkout,
        early_checkout=early_checkout,
        reassign_allowed=reassign_allowed,
        can_pay=can_pay,
        can_refund=can_refund,
        can_cancel_directly=can_cancel_directly,
        booking_requests=booking_requests,
        block_conflicts=block_conflicts,
        current_block=current_block,
        is_manager=is_manager,
        walk_allowed=walk_allowed,
        walk_reason=walk_reason,
        walk_form=walk_form,
        walk_customer=walk_customer,
        walk_matches=walk_matches,
        block_form=block_form,
        payment_form=PaymentForm(prefix="pay"),
        refund_form=RefundForm(prefix="ref"),
        reassign_form=ReassignForm(prefix="re"),
        decision_form=DecisionForm(prefix="req"),
        contact_form=ContactForm(prefix="con"),
        no_show_form=NoShowForm(prefix="ns"),
        cancel_form=CancelBookingForm(prefix="cx"),
        order_tab=order_tab,
        order_tabs=ORDER_TABS,
        status_filter=request.args.get("status") or "",
        order_rows=order_rows,
        task_rows=task_rows,
        desk_url=_desk_url,
        check_in_from=desk.CHECK_IN_FROM.strftime("%H:%M"),
        check_out_by=desk.CHECK_OUT_BY.strftime("%H:%M"),
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
    return _back(booking=booking_id)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/refund")
@desk_required
def refund(booking_id):
    booking = _booking_or_404(booking_id)
    form = RefundForm(prefix="ref")
    amount = form.amount.data if form.validate_on_submit() else None

    def action(session):
        desk.record_refund(session, session.get(Booking, booking_id), current_user.id, amount)

    _run(action, f"Refund recorded for {booking.reference}.")
    return _back(booking=booking_id)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/check-in")
@desk_required
def check_in(booking_id):
    booking = _booking_or_404(booking_id)
    now = hotel_now()

    def action(session):
        desk.check_in(session, booking_id, current_user.id, now)

    _run(action, f"{booking.reference} checked in.")
    return _back(booking=booking_id)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/check-out")
@desk_required
def check_out(booking_id):
    booking = _booking_or_404(booking_id)
    now = hotel_now()
    outcome = {}

    def action(session):
        outcome.update(desk.check_out(session, booking_id, current_user.id, now))

    _run(action, f"{booking.reference} checked out. The room is now marked dirty.")
    if outcome.get("overdue"):
        flash("Recorded after the planned departure date.", "info")
    if outcome.get("late"):
        flash("Recorded after 12:00, so this is a late check-out.", "info")
    if outcome.get("early"):
        flash("Departed early: the unused nights were released.", "info")
    return _back(booking=booking_id, room=None)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/cancel")
@desk_required
def cancel_booking(booking_id):
    booking = _booking_or_404(booking_id)
    if current_user.role != MANAGER:
        abort(403, description="Only a manager can cancel a confirmed booking directly.")
    form = CancelBookingForm(prefix="cx")
    if not form.validate_on_submit():
        flash("Give a reason before cancelling.", "error")
        return _back(booking=booking_id)
    now = hotel_now()
    source = "exception" if now.replace(tzinfo=None) > cancellation_deadline(booking) else "hotel"

    def action(session):
        try:
            amount = cancel_confirmed_booking(
                session, current_user.id, booking_id, source, form.reason.data, now,
            )
        except BookingRejected as exc:
            raise desk.DeskError(str(exc)) from exc
        flash(f"Simulated refund: ¥{amount:,.2f}.", "info")

    _run(action, f"{booking.reference} cancelled by manager authority.")
    return _back(booking=booking_id)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/contact")
@desk_required
def contact(booking_id):
    booking = _booking_or_404(booking_id)
    form = ContactForm(prefix="con")
    if not form.validate_on_submit():
        flash("Choose what happened when you contacted the guest.", "error")
        return _back(booking=booking_id)

    def action(session):
        desk.contact_customer(session, booking_id, current_user.id, form.result.data,
                             form.note.data, form.new_expected.data)

    _run(action, "Contact attempt recorded.")
    return _back(booking=booking_id)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/no-show")
@desk_required
def no_show(booking_id):
    booking = _booking_or_404(booking_id)
    form = NoShowForm(prefix="ns")
    now = hotel_now()
    if not form.validate_on_submit():
        flash("Give a reason before marking a no-show.", "error")
        return _back(booking=booking_id)

    def action(session):
        desk.mark_no_show(session, booking_id, current_user.id, form.reason.data, now)

    _run(action, f"{booking.reference} marked as a no-show and the room released.")
    return _back(booking=None, room=booking.room_id)


@frontdesk.post("/frontdesk/rooms/<int:room_id>/clean")
@desk_required
def mark_clean(room_id):
    room = db.session.get(Room, room_id)
    if room is None:
        abort(404)

    def action(session):
        desk.mark_room_clean(session, room_id, current_user.id)

    _run(action, f"Room {room.number} is clean and ready.")
    return _back()


@frontdesk.post("/frontdesk/walk-in")
@desk_required
def walk_in():
    form = WalkInForm(prefix="walk")
    form.room_type_id.choices = _room_type_choices()
    if not form.validate_on_submit():
        flash("Check the walk-in: guest, room type, dates and guests are required.", "error")
        return _back()

    now = hotel_now()
    stay = Stay(form.check_in.data, form.check_out.data, form.guests.data)
    if stay.check_in != now.date():
        flash("A walk-in starts today.", "error")
        return _back()

    booking_id = {}

    def action(session):
        booking = desk.create_walkin(
            session, current_user.id, form.room_type_id.data, stay,
            customer_id=int(form.customer_id.data) if form.customer_id.data else None,
            name=form.name.data, phone=form.phone.data, note=form.note.data,
            room_id=form.room_id.data,
        )
        booking_id["id"] = booking.id

    _run(action, "Walk-in booked, paid and checked in.")
    return _back(booking=booking_id.get("id"), room=None)


@frontdesk.post("/frontdesk/blocks")
@desk_required
def create_block():
    form = BlockForm(prefix="block")
    if not form.validate_on_submit():
        flash("Check the block: room, dates and a reason are required.", "error")
        return _back()

    conflicts = []
    room_id = form.room_id.data

    def action(session):
        _, found = desk.create_block(
            session, room_id, form.start_date.data, form.end_date.data,
            form.reason.data, current_user.id,
        )
        conflicts.extend(found)

    _run(action, "Out-of-service block saved.")
    if conflicts:
        flash(f"{len(conflicts)} booking(s) now conflict and need reassignment.", "info")
    return _back(room=room_id, booking=None)


@frontdesk.post("/frontdesk/blocks/<int:block_id>/cancel")
@desk_required
def cancel_block(block_id):
    block = db.session.get(RoomBlock, block_id)
    if block is None:
        abort(404)
    room_id = block.room_id

    def action(session):
        desk.cancel_block(session, block_id, current_user.id)

    _run(action, f"Block on room {block.room.number} cancelled.")
    return _back(room=room_id)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/reassign")
@desk_required
def reassign(booking_id):
    booking = _booking_or_404(booking_id)
    form = ReassignForm(prefix="re")
    if not form.validate_on_submit():
        flash("Give a reason for the reassignment.", "error")
        return _back(booking=booking_id)

    moved = {}

    def action(session):
        updated = desk.reassign(
            session, booking_id, current_user.id, form.reason.data,
            today=hotel_now().date(),
        )
        moved["room"] = updated.room.number

    _run(action, lambda: f"{booking.reference} moved to room {moved.get('room', '')}.")
    return _back(booking=booking_id)


@frontdesk.post("/frontdesk/requests/<int:request_id>/approve")
@desk_required
def approve_request(request_id):
    row = db.session.get(BookingRequest, request_id)
    if row is None:
        abort(404)
    form = DecisionForm(prefix="req")
    comment = form.comment.data if form.validate_on_submit() else None

    def action(session):
        desk.approve_request(session, request_id, current_user.id, comment,
                             actor_role=current_user.role, today=hotel_now().date())

    _run(action, "Request approved and applied.")
    return _back(booking=row.booking_id)


@frontdesk.post("/frontdesk/requests/<int:request_id>/reject")
@desk_required
def reject_request(request_id):
    row = db.session.get(BookingRequest, request_id)
    if row is None:
        abort(404)
    form = DecisionForm(prefix="req")
    comment = (form.comment.data or "").strip() if form.validate_on_submit() else ""
    if not comment:
        flash("A rejection needs a reason the guest can read.", "error")
        return _back(booking=row.booking_id)

    def action(session):
        desk.reject_request(session, request_id, current_user.id, comment)

    _run(action, "Request rejected; the booking is unchanged.")
    return _back(booking=row.booking_id)
