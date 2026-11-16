from datetime import datetime, time, timedelta
from functools import wraps
from uuid import uuid4

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import (
    DateField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
    TimeField,
)
from wtforms.validators import EqualTo, InputRequired, Length, NumberRange, Optional, Regexp
from wtforms.widgets import HiddenInput

from .audit import record_audit
from .booking import (
    BookingRejected,
    Stay,
    cancellation_deadline,
    create_booking,
    create_guest_request,
    customer_is_blocked,
    get_booking,
    hotel_now,
    listed_room_types,
    quote_token,
    recent_bookings,
    search_results,
    stay_error,
    update_booking_note,
    withdraw_guest_request,
)
from .extensions import db
from .models import Booking, BookingRequest, Customer, GUEST, PaymentRecord, RoomType, User, utc_now


guest = Blueprint("guest", __name__, url_prefix="/guest")

BED_TYPES = [
    ("all", "All"),
    ("single", "Single"),
    ("double", "Double"),
    ("twin", "Twin"),
    ("king", "King"),
    ("studio", "Studio"),
    ("suite", "Suite"),
]


class SearchForm(FlaskForm):
    check_in = DateField("Check-in", validators=[InputRequired()])
    check_out = DateField("Check-out", validators=[InputRequired()])
    submit = SubmitField("Search rooms")


class BookForm(FlaskForm):
    room_type_id = IntegerField(widget=HiddenInput(), validators=[InputRequired()])
    check_in = DateField(widget=HiddenInput(), validators=[InputRequired()])
    check_out = DateField(widget=HiddenInput(), validators=[InputRequired()])
    guests = IntegerField("Guests", validators=[InputRequired(), NumberRange(min=1, max=2147483647)])
    expected_arrival = TimeField("Expected arrival", format="%H:%M", validators=[InputRequired()])
    note = TextAreaField("Note (optional)", validators=[Optional()])
    idempotency_key = StringField(widget=HiddenInput(), validators=[InputRequired(), Length(max=64)])
    quote = StringField(widget=HiddenInput(), validators=[InputRequired()])
    submit = SubmitField("Confirm booking")


class NoteForm(FlaskForm):
    note = TextAreaField("Note", validators=[Optional()])
    submit = SubmitField("Save note")


class ProfileForm(FlaskForm):
    name = StringField("Name", validators=[InputRequired(), Length(min=1, max=80)])
    phone = StringField(
        "Phone",
        validators=[
            InputRequired(),
            Regexp(r"^[0-9+\- ]{6,20}$", message="Enter a phone number with 6 to 20 digits."),
        ],
    )
    submit = SubmitField("Save details")


class PasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[InputRequired(), Length(max=128)])
    new_password = PasswordField("New password", validators=[InputRequired(), Length(min=8, max=128)])
    confirm = PasswordField(
        "Confirm new password",
        validators=[InputRequired(), EqualTo("new_password", message="Passwords must match.")],
    )
    submit = SubmitField("Change password")


class ReasonForm(FlaskForm):
    reason = TextAreaField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Submit request")


class RoomTypeRequestForm(ReasonForm):
    target_room_type_id = SelectField("New room type", coerce=int, validators=[InputRequired()])


class DatesRequestForm(ReasonForm):
    check_in = DateField("New check-in", validators=[InputRequired()])
    check_out = DateField("New check-out", validators=[InputRequired()])


class ExtensionRequestForm(ReasonForm):
    check_out = DateField("New check-out", validators=[InputRequired()])


class CancelRequestForm(ReasonForm):
    pass


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
        return None, "Enter valid check-in and check-out dates."
    stay = Stay(form.check_in.data, form.check_out.data, 1)
    return stay, stay_error(stay, now)


def requested_guests(default=1):
    """Guest count is chosen on the confirmation page, not in the home search."""
    return max(1, request.args.get("guests", type=int) or default)


def search_redirect(stay):
    return redirect(
        url_for(
            "guest.index",
            check_in=stay.check_in.isoformat(),
            check_out=stay.check_out.isoformat(),
        )
    )


def booking_or_404(booking_id):
    booking = get_booking(db.session, booking_id, current_user.id)
    if booking is None:
        abort(404)
    return booking


@guest.app_context_processor
def guest_nav_context():
    if not current_user.is_authenticated or current_user.role != GUEST:
        return {}
    count = db.session.scalar(
        db.select(func.count(BookingRequest.id))
        .select_from(BookingRequest)
        .join(Booking, BookingRequest.booking_id == Booking.id)
        .join(Customer, Booking.customer_id == Customer.id)
        .where(Customer.user_id == current_user.id, BookingRequest.status == "pending")
    )
    return {"guest_pending_requests": count or 0}


@guest.get("/")
@guest_required
def index():
    now = hotel_now()
    selected_bed_type = request.args.get("bed_type", "all")
    if selected_bed_type not in {value for value, _ in BED_TYPES}:
        selected_bed_type = "all"
    searched = any(key in request.args for key in ("check_in", "check_out"))
    form = SearchForm(request.args if searched else None, meta={"csrf": False})
    if not searched:
        form.check_in.data = now.date() + timedelta(days=2)
        form.check_out.data = now.date() + timedelta(days=4)

    error = None
    stay = None
    offers = []
    if searched:
        stay, error = parse_search(form, now)
        if not error:
            quotes = search_results(db.session, stay)
            offers = [
                {"room_type": quote.room_type, "total": quote.total, "rates": quote.rates}
                for quote in quotes
            ]
    else:
        room_types = listed_room_types(db.session)
        offers = [
            {"room_type": room_type, "total": None, "rates": []}
            for room_type in room_types
        ]

    return render_template(
        "guest/index.html",
        form=form,
        searched=searched,
        error=error,
        stay=stay,
        offers=offers,
        bed_types=BED_TYPES,
        selected_bed_type=selected_bed_type,
        recent=recent_bookings(db.session, current_user.id, 3),
        today=now.date(),
        tomorrow=now.date() + timedelta(days=1),
        after_cutoff=now.time() >= time(22),
        last_arrival=now.date() + timedelta(days=14),
    ), 400 if error else 200


@guest.get("/book/<int:room_type_id>")
@guest_required
def confirm(room_type_id):
    now = hotel_now()
    form = SearchForm(request.args, meta={"csrf": False})
    if not form.validate():
        flash("Enter valid check-in and check-out dates.", "error")
        return redirect(url_for("guest.index"))

    stay = Stay(form.check_in.data, form.check_out.data, requested_guests())
    error = stay_error(stay, now)
    if error:
        flash(error, "error")
        return search_redirect(stay)

    room_type = db.session.get(RoomType, room_type_id)
    if room_type is None or not room_type.listed:
        flash("That room type is no longer available for these dates.", "error")
        return search_redirect(stay)
    if room_type.capacity < stay.guests:
        flash(
            f"That room type sleeps up to {room_type.capacity} guest"
            f"{'' if room_type.capacity == 1 else 's'}. Lower the guest count or pick another room.",
            "error",
        )
        return search_redirect(stay)

    quote = next(
        (item for item in search_results(db.session, stay) if item.room_type.id == room_type_id),
        None,
    )
    if quote is None:
        flash("No room of that type covers the whole stay.", "error")
        return search_redirect(stay)

    book_form = BookForm()
    book_form.room_type_id.data = room_type.id
    book_form.check_in.data = stay.check_in
    book_form.check_out.data = stay.check_out
    book_form.guests.data = stay.guests
    default_arrival = time(15, 0)
    if stay.check_in == now.date():
        default_arrival = max(default_arrival, now.time().replace(second=0, microsecond=0))
    book_form.expected_arrival.data = default_arrival
    book_form.idempotency_key.data = str(uuid4())
    book_form.quote.data = quote_token(room_type.id, stay, quote.rates)

    return render_template(
        "guest/confirm.html",
        form=book_form,
        room_type=room_type,
        stay=stay,
        quote=quote,
        cancellation_deadline=datetime.combine(stay.check_in - timedelta(days=1), time(18)),
        deadline_passed=now.replace(tzinfo=None) > datetime.combine(
            stay.check_in - timedelta(days=1), time(18)
        ),
    )


@guest.post("/book")
@guest_required
def book():
    form = BookForm()
    if not form.validate_on_submit():
        flash("Check the booking form and try again.", "error")
        return redirect(url_for("guest.index"))

    stay = Stay(form.check_in.data, form.check_out.data, form.guests.data)
    room_type = db.session.get(RoomType, form.room_type_id.data)
    if room_type is None or not room_type.listed:
        flash("That room type is no longer available for these dates.", "error")
        return search_redirect(stay)
    if room_type.capacity < stay.guests:
        flash(
            f"That room type sleeps up to {room_type.capacity} guest"
            f"{'' if room_type.capacity == 1 else 's'}. Lower the guest count or pick another room.",
            "error",
        )
        return redirect(
            url_for(
                "guest.confirm",
                room_type_id=room_type.id,
                check_in=stay.check_in.isoformat(),
                check_out=stay.check_out.isoformat(),
                guests=room_type.capacity,
            )
        )
    try:
        result = create_booking(
            current_user.id,
            form.room_type_id.data,
            stay,
            form.idempotency_key.data,
            note=form.note.data,
            confirmed_quote=form.quote.data,
            expected_arrival=form.expected_arrival.data,
        )
    except BookingRejected as exc:
        flash(str(exc), "error")
        if exc.code == "PRICE_CHANGED":
            return redirect(
                url_for(
                    "guest.confirm",
                    room_type_id=form.room_type_id.data,
                    check_in=stay.check_in.isoformat(),
                    check_out=stay.check_out.isoformat(),
                    guests=stay.guests,
                )
            )
        return search_redirect(stay)

    if result.status == "confirmed":
        flash(f"Booking BK-{result.id:06d} is confirmed.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=result.id))


@guest.get("/bookings")
@guest_required
def bookings():
    selected = request.args.get("status", "all")
    if selected not in {"all", "upcoming", "in_house", "past", "cancelled", "pending"}:
        selected = "all"

    own = db.session.scalars(
        db.select(Booking)
        .where(Booking.customer.has(Customer.user_id == current_user.id))
        .order_by(Booking.created_at.desc(), Booking.id.desc())
    ).all()
    pending_ids = set()
    if own:
        pending_ids = set(db.session.scalars(
            db.select(BookingRequest.booking_id).where(
                BookingRequest.booking_id.in_([booking.id for booking in own]),
                BookingRequest.status == "pending",
            )
        ).all())

    counts = {
        "all": len(own),
        "upcoming": sum(1 for booking in own if booking.status == "confirmed"),
        "in_house": sum(1 for booking in own if booking.status == "checked_in"),
        "past": sum(1 for booking in own if booking.status in ("checked_out", "no_show")),
        "cancelled": sum(1 for booking in own if booking.status == "cancelled"),
        "pending": len(pending_ids),
    }
    if selected == "all":
        shown = own
    elif selected == "upcoming":
        shown = [booking for booking in own if booking.status == "confirmed"]
    elif selected == "in_house":
        shown = [booking for booking in own if booking.status == "checked_in"]
    elif selected == "past":
        shown = [booking for booking in own if booking.status in ("checked_out", "no_show")]
    elif selected == "pending":
        shown = [booking for booking in own if booking.id in pending_ids]
    else:
        shown = [booking for booking in own if booking.status == "cancelled"]

    return render_template(
        "guest/bookings.html",
        bookings=shown,
        selected=selected,
        counts=counts,
        pending_ids=pending_ids,
    )


REQUEST_ERROR_KEY = "booking_request_error"


def remember_request_error(booking_id, message):
    """Carry a refused request back to the detail page so the guest sees why.

    The request form is a POST that redirects; a flash alone scrolls past unnoticed,
    so the message is stored for one render and shown inside the actions panel.
    """

    session[REQUEST_ERROR_KEY] = {"booking_id": booking_id, "message": message}


def take_request_error(booking_id):
    remembered = session.pop(REQUEST_ERROR_KEY, None)
    if remembered and remembered.get("booking_id") == booking_id:
        return remembered.get("message")
    return None


@guest.get("/bookings/<int:booking_id>")
@guest_required
def booking_detail(booking_id):
    booking = booking_or_404(booking_id)
    payments = db.session.scalars(
        db.select(PaymentRecord).where(
            PaymentRecord.booking_id == booking.id, PaymentRecord.result == "success",
        ).order_by(PaymentRecord.id)
    ).all()
    changes = db.session.scalars(
        db.select(BookingRequest)
        .where(BookingRequest.booking_id == booking.id)
        .order_by(BookingRequest.id.desc())
    ).all()
    pending = next((change for change in changes if change.status == "pending"), None)

    now = hotel_now()
    deadline = cancellation_deadline(booking)
    can_request = booking.status == "confirmed" and booking.checked_in_at is None and pending is None
    can_extend = booking.status in ("confirmed", "checked_in") and pending is None
    can_cancel = can_request and now.replace(tzinfo=None) <= deadline

    room_type_choices = db.session.scalars(
        db.select(RoomType)
        .where(RoomType.listed.is_(True), RoomType.id != booking.room_type_id)
        .order_by(RoomType.capacity, RoomType.name)
    ).all()
    room_type_form = RoomTypeRequestForm()
    room_type_form.target_room_type_id.choices = [
        (room_type.id, f"{room_type.name} · ¥{room_type.base_price}")
        for room_type in room_type_choices
    ]
    dates_form = DatesRequestForm()
    dates_form.check_in.data = booking.check_in
    dates_form.check_out.data = booking.check_out
    extension_form = ExtensionRequestForm()
    extension_form.check_out.data = booking.check_out + timedelta(days=1)
    cancel_form = CancelRequestForm()
    note_form = NoteForm()
    note_form.note.data = booking.note
    request_error = take_request_error(booking.id)

    return render_template(
        "guest/booking_detail.html",
        booking=booking,
        payments=payments,
        changes=changes,
        pending=pending,
        deadline=deadline,
        deadline_passed=now.replace(tzinfo=None) > deadline,
        now=now,
        can_request=can_request,
        can_extend=can_extend,
        can_cancel=can_cancel,
        note_editable=booking.status in ("confirmed", "checked_in"),
        note_form=note_form,
        room_type_form=room_type_form,
        room_type_choices=room_type_choices,
        dates_form=dates_form,
        extension_form=extension_form,
        cancel_form=cancel_form,
        request_error=request_error,
    )


@guest.post("/bookings/<int:booking_id>/note")
@guest_required
def update_note(booking_id):
    booking_or_404(booking_id)
    form = NoteForm()
    if not form.validate_on_submit():
        flash("The note must be 100 characters or fewer.", "error")
        return redirect(url_for("guest.booking_detail", booking_id=booking_id))
    try:
        update_booking_note(current_user.id, booking_id, form.note.data)
    except BookingRejected as exc:
        flash(str(exc), "error")
    else:
        flash("Note updated.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=booking_id))


@guest.post("/bookings/<int:booking_id>/requests/room-type")
@guest_required
def request_room_type(booking_id):
    booking_or_404(booking_id)
    form = RoomTypeRequestForm()
    form.target_room_type_id.choices = [
        (room_type.id, room_type.name)
        for room_type in db.session.scalars(
            db.select(RoomType).where(RoomType.listed.is_(True)).order_by(RoomType.name)
        ).all()
    ]
    if not form.validate_on_submit():
        remember_request_error(booking_id, "Choose a room type and give a reason.")
        return redirect(url_for("guest.booking_detail", booking_id=booking_id))
    try:
        create_guest_request(
            current_user.id,
            booking_id,
            "room_type",
            {"room_type_id": form.target_room_type_id.data},
            form.reason.data,
        )
    except BookingRejected as exc:
        remember_request_error(booking_id, str(exc))
    else:
        flash("Room-type request submitted. The original booking is unchanged.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=booking_id))


@guest.post("/bookings/<int:booking_id>/requests/dates")
@guest_required
def request_dates(booking_id):
    booking_or_404(booking_id)
    form = DatesRequestForm()
    if not form.validate_on_submit():
        remember_request_error(booking_id, "Enter new dates and a reason. "
                               "The dates must differ from the current stay.")
        return redirect(url_for("guest.booking_detail", booking_id=booking_id))
    try:
        create_guest_request(
            current_user.id,
            booking_id,
            "dates",
            {
                "check_in": form.check_in.data.isoformat(),
                "check_out": form.check_out.data.isoformat(),
            },
            form.reason.data,
        )
    except BookingRejected as exc:
        remember_request_error(booking_id, str(exc))
    else:
        flash("Date-change request submitted. The original booking is unchanged.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=booking_id))


@guest.post("/bookings/<int:booking_id>/requests/extension")
@guest_required
def request_extension(booking_id):
    booking_or_404(booking_id)
    form = ExtensionRequestForm()
    if not form.validate_on_submit():
        remember_request_error(booking_id, "Enter a new check-out date and a reason.")
        return redirect(url_for("guest.booking_detail", booking_id=booking_id))
    try:
        create_guest_request(
            current_user.id,
            booking_id,
            "extend",
            {"check_out": form.check_out.data.isoformat()},
            form.reason.data,
        )
    except BookingRejected as exc:
        remember_request_error(booking_id, str(exc))
    else:
        flash("Extension request submitted. The original booking is unchanged.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=booking_id))


@guest.post("/bookings/<int:booking_id>/requests/cancel")
@guest_required
def request_cancel(booking_id):
    booking_or_404(booking_id)
    form = CancelRequestForm()
    if not form.validate_on_submit():
        remember_request_error(booking_id, "Give a reason for the cancellation request.")
        return redirect(url_for("guest.booking_detail", booking_id=booking_id))
    try:
        create_guest_request(current_user.id, booking_id, "cancel", {}, form.reason.data)
    except BookingRejected as exc:
        remember_request_error(booking_id, str(exc))
    else:
        flash("Cancellation request submitted. The booking is unchanged until it is approved.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=booking_id))


@guest.post("/bookings/<int:booking_id>/requests/<int:request_id>/withdraw")
@guest_required
def withdraw_request(booking_id, request_id):
    booking_or_404(booking_id)
    try:
        withdraw_guest_request(current_user.id, booking_id, request_id)
    except BookingRejected as exc:
        remember_request_error(booking_id, str(exc))
    else:
        flash("Request withdrawn. The booking was not changed.", "success")
    return redirect(url_for("guest.booking_detail", booking_id=booking_id))


@guest.get("/account")
@guest_required
def account():
    customer = db.session.scalar(
        db.select(Customer).where(Customer.user_id == current_user.id)
    )
    blocked = bool(
        customer and customer_is_blocked(db.session, customer.id, utc_now())
    )
    profile_form = ProfileForm()
    if customer is not None:
        profile_form.name.data = customer.name
        profile_form.phone.data = customer.phone
    return render_template(
        "guest/account.html",
        customer=customer,
        blocked=blocked,
        profile_form=profile_form,
        form=PasswordForm(),
    )


@guest.post("/account/profile")
@guest_required
def update_profile():
    form = ProfileForm()
    if not form.validate_on_submit():
        flash("Enter a name and a phone number the hotel can reach.", "error")
        return redirect(url_for("guest.account"))

    name = form.name.data.strip()
    phone = form.phone.data.strip()
    with Session(db.engine) as session, session.begin():
        customer = session.scalar(
            db.select(Customer).where(Customer.user_id == current_user.id).with_for_update()
        )
        if customer is None:
            flash("This account has no guest profile yet.", "error")
            return redirect(url_for("guest.account"))
        before = {"name": customer.name, "phone": customer.phone}
        customer.name = name
        customer.phone = phone
        record_audit(
            session, current_user.id, "update_profile", "customer", customer.id,
            details={"before": before, "after": {"name": name, "phone": phone}},
        )
    flash("Your contact details were updated.", "success")
    return redirect(url_for("guest.account"))


@guest.post("/account/password")
@guest_required
def change_password():
    form = PasswordForm()
    if not form.validate_on_submit():
        flash("Check the password form and try again.", "error")
        return redirect(url_for("guest.account"))

    changed = False
    with Session(db.engine) as session, session.begin():
        user = session.get(User, current_user.id, with_for_update=True)
        if user and user.active and check_password_hash(user.password_hash, form.current_password.data):
            user.password_hash = generate_password_hash(form.new_password.data)
            record_audit(session, user.id, "change_password", "user", user.id)
            changed = True

    if changed:
        flash("Password updated.", "success")
    else:
        flash("The current password is incorrect.", "error")
    return redirect(url_for("guest.account"))
