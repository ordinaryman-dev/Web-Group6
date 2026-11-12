import csv
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload
from wtforms import SelectChoice

from . import manager_services as services
from .auth import role_required
from .booking import BookingRejected, HOTEL_ZONE, cancel_confirmed_booking, cancellation_deadline, hotel_now, refundable_amount
from .extensions import db
from .manager_forms import BlacklistForm, CancelForm, PasswordForm, PriceForm, ReasonForm, RoomForm, RoomTypeForm, StaffForm
from .models import (
    AuditLog, BlacklistEntry, Booking, BookingRequest, Customer, MANAGER, PaymentRecord, PriceRule,
    Room, RoomBlock, RoomNight, RoomType, STAFF, User,
)
from .reports import DAY, occupancy_report, report_range


manager = Blueprint("manager", __name__, url_prefix="/manage")
BOOKING_STATUSES = ("confirmed", "checked_in", "checked_out", "cancelled", "no_show")


@manager.before_request
@role_required(MANAGER)
def authorize_manager():
    pass


def run_write(action, *args, change_own_password=False):
    actor_id = current_user.id
    try:
        with services.manager_transaction(actor_id, change_own_password=change_own_password) as session:
            result = action(session, actor_id, *args)
    except BookingRejected as exc:
        status = {"NOT_FOUND": 404, "INVALID_REQUEST": 400}.get(exc.code, 409)
        raise services.ManagerRejected(str(exc), status) from exc
    except IntegrityError as exc:
        if exc.orig.args and exc.orig.args[0] == 1062:
            raise services.ManagerRejected("That code, room number or email already exists.") from exc
        raise
    except OperationalError as exc:
        if exc.orig.args and exc.orig.args[0] in (1205, 1213):
            raise services.ManagerRejected("Another operation is updating this record. Try again.") from exc
        raise
    return result


def show_error(error):
    if error.status in (403, 404):
        abort(error.status, description=str(error))
    flash(str(error), "error")


def edit_form(form, title, endpoint, message, operation, *args, change_own_password=False):
    status = 200
    if form.validate_on_submit():
        try:
            run_write(operation, *args, change_own_password=change_own_password)
        except services.ManagerRejected as error:
            show_error(error)
            status = error.status
        else:
            flash(message, "success")
            return redirect(url_for(endpoint))
    elif form.is_submitted():
        status = 400
    return render_template("manager/form.html", form=form, title=title, back=url_for(endpoint)), status


def row_action(operation, message, endpoint, *args):
    try:
        run_write(operation, *args)
    except services.ManagerRejected as error:
        show_error(error)
    else:
        flash(message, "success")
    return redirect(url_for(endpoint))


def count_rows(model, *conditions):
    return db.session.scalar(select(func.count()).select_from(model).where(*conditions))


@manager.get("", strict_slashes=False)
def overview():
    now = hotel_now()
    today = now.date()
    start, end = report_range("week", today, {})
    weekly = occupancy_report(db.session, start, end, today)
    trend = occupancy_report(db.session, today - timedelta(days=14), today, today, daily=True)
    latest = db.session.scalars(
        select(AuditLog).options(joinedload(AuditLog.actor)).order_by(AuditLog.id.desc()).limit(6)
    ).all()
    last_late_arrival = today + DAY if now.time() > time(18) else today
    candidate = db.session.scalar(select(Booking).where(
        Booking.status == "confirmed", Booking.checked_in_at.is_(None), Booking.check_in <= last_late_arrival,
    ).order_by(Booking.check_in.desc(), Booking.id.desc()).limit(1))
    return render_template(
        "manager/overview.html", now=now, weekly=weekly, trend=trend, latest=latest,
        in_house=count_rows(Booking, Booking.status == "checked_in"),
        arrivals=count_rows(Booking, Booking.status == "confirmed", Booking.check_in == today),
        pending=count_rows(BookingRequest, BookingRequest.status == "pending"),
        candidate=candidate, refund=refundable_amount(db.session, candidate.id) if candidate else None,
    )


@manager.get("/room-types")
def room_types():
    items = db.session.scalars(select(RoomType).order_by(RoomType.name, RoomType.id)).all()
    return render_template("manager/catalogue.html", section="types", items=items)


@manager.route("/room-types/new", defaults={"record_id": None}, methods=["GET", "POST"])
@manager.route("/room-types/<int:record_id>", methods=["GET", "POST"])
def room_type_form(record_id):
    room_type = db.get_or_404(RoomType, record_id) if record_id is not None else None
    form = RoomTypeForm(obj=room_type)
    return edit_form(
        form, "Edit room type" if room_type else "New room type", "manager.room_types",
        "Room type saved.", services.save_room_type, record_id, form,
    )


def room_type_choices():
    return [SelectChoice(type_id, name) for type_id, name in db.session.execute(
        select(RoomType.id, RoomType.name).order_by(RoomType.name, RoomType.id)
    )]


@manager.get("/rooms")
def rooms():
    today = hotel_now().date()
    raw_day = request.args.get("day")
    try:
        day = date.fromisoformat(raw_day) if raw_day else today
    except ValueError:
        abort(400, description="Enter a valid room-status date.")
    items = db.session.execute(select(Room, RoomType.name).join(RoomType).order_by(Room.number)).all()
    blocked = set(db.session.scalars(select(RoomBlock.room_id).where(
        RoomBlock.start_date <= day, RoomBlock.end_date > day,
    )))
    bookings_by_room = dict(db.session.execute(
        select(RoomNight.room_id, Booking.status).join(Booking).where(RoomNight.stay_date == day)
    ).all())
    return render_template(
        "manager/catalogue.html", section="rooms", items=items, day=day, today=today,
        blocked=blocked, bookings_by_room=bookings_by_room,
    )


@manager.route("/rooms/new", defaults={"record_id": None}, methods=["GET", "POST"])
@manager.route("/rooms/<int:record_id>", methods=["GET", "POST"])
def room_form(record_id):
    today = hotel_now().date()
    room = db.get_or_404(Room, record_id) if record_id is not None else None
    form = RoomForm(obj=room)
    form.room_type_id.choices = room_type_choices()
    if room is None and not form.is_submitted():
        form.operational_from.data = today
    return edit_form(
        form, "Edit room" if room else "New room", "manager.rooms",
        "Room saved.", services.save_room, record_id, form, today,
    )


@manager.get("/prices")
def prices():
    items = db.session.execute(
        select(PriceRule, RoomType.name).join(RoomType)
        .order_by(PriceRule.active.desc(), RoomType.name, PriceRule.start_date.desc(), PriceRule.id.desc())
    ).all()
    return render_template("manager/catalogue.html", section="prices", items=items)


@manager.route("/prices/new", defaults={"rule_id": None}, methods=["GET", "POST"])
@manager.route("/prices/<int:rule_id>", methods=["GET", "POST"])
def price_form(rule_id):
    rule = db.get_or_404(PriceRule, rule_id) if rule_id is not None else None
    if rule and not rule.active:
        abort(404)
    form = PriceForm(obj=rule)
    form.room_type_id.choices = [choice for choice in room_type_choices() if rule is None or choice.value == rule.room_type_id]
    return edit_form(
        form, "Edit price rule" if rule else "New price rule", "manager.prices",
        "Price rule saved.", services.save_price_rule, rule_id, form.room_type_id.data, form,
    )


@manager.post("/prices/<int:rule_id>/retire")
def retire_price(rule_id):
    rule = db.get_or_404(PriceRule, rule_id)
    return row_action(services.retire_price_rule, "Price rule retired.", "manager.prices", rule.room_type_id, rule_id)


@manager.get("/accounts")
def accounts():
    staff = db.session.scalars(select(User).where(User.role == STAFF).order_by(User.email)).all()
    return render_template("manager/accounts.html", staff=staff)


@manager.route("/accounts/new", methods=["GET", "POST"])
def staff_form():
    form = StaffForm()
    return edit_form(
        form, "New front-desk account", "manager.accounts", "Staff account created.",
        services.create_staff, form.email.data, form.password.data,
    )


@manager.post("/accounts/<int:staff_id>/disable")
def disable_staff(staff_id):
    return row_action(services.disable_staff, "Staff account is inactive.", "manager.accounts", staff_id)


@manager.route("/profile", methods=["GET", "POST"])
def profile():
    form = PasswordForm()
    return edit_form(
        form, "My profile", "manager.profile", "Password updated.", services.change_password,
        form.current_password.data, form.new_password.data, change_own_password=True,
    )


@manager.route("/blacklist", methods=["GET", "POST"])
def blacklist():
    form = BlacklistForm()
    status = 200
    if form.validate_on_submit():
        try:
            run_write(services.add_blacklist, form.customer_id.data, form.reason.data)
        except services.ManagerRejected as error:
            show_error(error)
            status = error.status
        else:
            flash("Customer added to the blacklist.", "success")
            return redirect(url_for("manager.blacklist"))
    elif form.is_submitted():
        status = 400
    else:
        form.customer_id.data = request.args.get("customer", type=int)
    search = request.args.get("q", "").strip()
    customers = select(Customer).order_by(Customer.name, Customer.id).limit(30)
    if search:
        customers = customers.outerjoin(User).where(or_(
            Customer.name.contains(search, autoescape=True), Customer.phone.contains(search, autoescape=True),
            User.email.contains(search, autoescape=True),
        ))
    entries = db.paginate(
        select(BlacklistEntry).options(joinedload(BlacklistEntry.customer)).order_by(BlacklistEntry.id.desc()),
        max_per_page=25, per_page=25, error_out=False,
    )
    return render_template(
        "manager/blacklist.html", form=form, search=search, entries=entries,
        customers=db.session.scalars(customers).all(),
    ), status


@manager.route("/blacklist/<int:customer_id>/remove", methods=["GET", "POST"])
def remove_blacklist(customer_id):
    db.get_or_404(Customer, customer_id)
    form = ReasonForm()
    return edit_form(
        form, "Remove from blacklist", "manager.blacklist", "Customer removed from the blacklist.",
        services.remove_blacklist, customer_id, form.reason.data,
    )


@manager.get("/bookings")
def bookings():
    status = request.args.get("status", "")
    query = request.args.get("q", "").strip()
    statement = select(Booking).join(Customer).order_by(Booking.id.desc())
    if status in BOOKING_STATUSES:
        statement = statement.where(Booking.status == status)
    if query:
        matching = Customer.name.contains(query, autoescape=True)
        number = query.upper().removeprefix("BK-")
        if number.isascii() and number.isdigit() and len(number) <= 10 and int(number) <= 2147483647:
            matching = or_(matching, Booking.id == int(number))
        statement = statement.where(matching)
    page = db.paginate(statement, max_per_page=25, per_page=25, error_out=False)
    return render_template("manager/bookings.html", page=page, query=query, status=status, statuses=BOOKING_STATUSES)


def render_booking(booking, form, status=200):
    now = hotel_now()
    payments = db.session.scalars(select(PaymentRecord).where(
        PaymentRecord.booking_id == booking.id,
    ).order_by(PaymentRecord.id)).all()
    changes = db.session.scalars(select(BookingRequest).where(
        BookingRequest.booking_id == booking.id,
    ).order_by(BookingRequest.id.desc())).all()
    return render_template(
        "manager/booking.html", booking=booking, form=form, payments=payments, changes=changes,
        deadline=cancellation_deadline(booking),
        exception_available=now.replace(tzinfo=None) > cancellation_deadline(booking),
        refundable=refundable_amount(db.session, booking.id),
    ), status


@manager.get("/bookings/<int:booking_id>")
def booking_detail(booking_id):
    return render_booking(db.get_or_404(Booking, booking_id), CancelForm())


@manager.post("/bookings/<int:booking_id>/cancel")
def cancel_booking(booking_id):
    booking = db.get_or_404(Booking, booking_id)
    form = CancelForm()
    if not form.validate_on_submit():
        return render_booking(booking, form, 400)
    try:
        amount = run_write(
            cancel_confirmed_booking, booking_id, form.source.data, form.reason.data, hotel_now(),
        )
    except services.ManagerRejected as error:
        show_error(error)
        db.session.rollback()
        return render_booking(db.get_or_404(Booking, booking_id), form, error.status)
    flash(f"Booking cancelled. Simulated refund: ¥{amount:,.2f}.", "success")
    return redirect(url_for("manager.booking_detail", booking_id=booking_id))


def current_report():
    today = hotel_now().date()
    period = request.args.get("period", "week")
    try:
        start, end = report_range(period, today, request.args)
    except ValueError as error:
        abort(400, description=str(error))
    return occupancy_report(db.session, start, end, today), period, end - DAY


@manager.get("/reports")
def reports():
    report, period, last_day = current_report()
    return render_template(
        "manager/reports.html", report=report, period=period, last_day=last_day, today=hotel_now().date(),
    )


@manager.get("/reports.csv")
def export_report():
    report, _, last_day = current_report()
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Period start", "Period end (inclusive)", "Time zone", "Room type", "Sellable past room nights",
        "Occupied room nights", "Occupancy (%)", "Upcoming reserved room nights", "Average confirmed nightly price (CNY)",
    ])
    for row in [report.total, *report.rows]:
        formula = row.name.lstrip().startswith(("=", "+", "-", "@")) or row.name.startswith(("\t", "\r", "\n"))
        writer.writerow([
            report.start, last_day, "Asia/Shanghai", "'" + row.name if formula else row.name,
            row.sellable, row.occupied, f"{row.occupancy:.1f}" if row.occupancy is not None else "", row.reserved,
            f"{row.average_price:.2f}" if row.average_price is not None else "",
        ])
    return Response(
        output.getvalue().encode("utf-8-sig"), content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="occupancy-{report.start}-{last_day}.csv"'},
    )


@manager.get("/audit")
def audit():
    filters = {name: request.args.get(name, "").strip() for name in ("actor", "action", "object_type", "object_id", "start", "end")}
    statement = select(AuditLog).join(User).options(joinedload(AuditLog.actor)).order_by(
        AuditLog.occurred_at.desc(), AuditLog.id.desc(),
    )
    if filters["actor"]:
        statement = statement.where(User.email.contains(filters["actor"], autoescape=True))
    for field in ("action", "object_type"):
        if filters[field]:
            statement = statement.where(getattr(AuditLog, field) == filters[field])
    if filters["object_id"]:
        try:
            object_id = int(filters["object_id"])
            if not 1 <= object_id <= 2147483647:
                raise ValueError
        except ValueError:
            abort(400, description="Enter a valid object ID.")
        statement = statement.where(AuditLog.object_id == object_id)
    try:
        for field in ("start", "end"):
            if filters[field]:
                day = date.fromisoformat(filters[field]) + (DAY if field == "end" else timedelta())
                utc = datetime.combine(day, time.min, HOTEL_ZONE).astimezone(timezone.utc).replace(tzinfo=None)
                statement = statement.where(AuditLog.occurred_at >= utc if field == "start" else AuditLog.occurred_at < utc)
    except (ValueError, OverflowError):
        abort(400, description="Enter valid audit dates.")
    page = db.paginate(statement, max_per_page=25, per_page=25, error_out=False)
    actions = db.session.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()
    objects = db.session.scalars(select(AuditLog.object_type).distinct().order_by(AuditLog.object_type)).all()
    return render_template("manager/audit.html", page=page, filters=filters, actions=actions, objects=objects)
