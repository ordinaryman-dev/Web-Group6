import csv
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from itertools import groupby

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import Numeric, case, cast, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload, lazyload
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
from .room_numbers import floor_key, floor_label, floor_sort


manager = Blueprint("manager", __name__, url_prefix="/manage")
BOOKING_STATUSES = ("confirmed", "checked_in", "checked_out", "cancelled", "no_show")
ROOM_STATUSES = (
    "available", "reserved", "occupied", "departing", "dirty",
    "maintenance", "maintenance_conflict", "out_of_service",
)
PAGE_SIZES = (25, 50, 100)


def list_options(sorts, default, *, prefix=""):
    sort = request.args.get(prefix + "sort", default)
    orders = {key: columns for key, _, *columns in sorts}
    if sort not in orders:
        sort = default
    size = request.args.get(prefix + "per_page", type=int)
    return {
        "sort": sort,
        "per_page": size if size in PAGE_SIZES else PAGE_SIZES[0],
        "sorts": [(key, label) for key, label, *_ in sorts],
        "order": orders[sort],
        "prefix": prefix,
    }


def list_page(statement, options):
    page_number = request.args.get(options["prefix"] + "page", 1, type=int)
    page = db.paginate(
        statement.order_by(*options["order"]), page=max(1, min(page_number, 1_000_000)),
        per_page=options["per_page"], error_out=False,
    )
    if page.page > max(1, page.pages):
        page = db.paginate(
            statement.order_by(*options["order"]), page=max(1, page.pages),
            per_page=options["per_page"], error_out=False,
        )
    return page


@manager.context_processor
def list_helpers():
    return {"list_url": list_url, "list_query": list_query()}


def search_query(name="q"):
    return request.args.get(name, "").strip()[:254]


def contains_any(query, *columns):
    return or_(*(column.icontains(query, autoescape=True) for column in columns))


def list_query():
    names = (
        "q", "day", "floor", "type", "bed", "window", "status", "sort", "per_page", "page",
        "customer", "customer_page", "customer_sort", "customer_per_page", "history_page",
        "history_sort", "history_per_page", "history_q", "history_status",
        "actor", "action", "object_type", "object_id", "start", "end",
    )
    return {name: request.args[name] for name in names if name in request.args}


def list_url(endpoint, **values):
    return url_for(endpoint, **(list_query() | values))


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
            return redirect(list_url(endpoint))
    elif form.is_submitted():
        status = 400
    return render_template("manager/form.html", form=form, title=title, back=list_url(endpoint)), status


def row_action(operation, message, endpoint, *args):
    try:
        run_write(operation, *args)
    except services.ManagerRejected as error:
        show_error(error)
    else:
        flash(message, "success")
    return redirect(list_url(endpoint))


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
    query = search_query()
    status = request.args.get("status", "")
    options = list_options((
        ("name", "Name A–Z", RoomType.name, RoomType.id),
        ("name_desc", "Name Z–A", RoomType.name.desc(), RoomType.id),
        ("capacity", "Capacity: highest first", RoomType.capacity.desc(), RoomType.name, RoomType.id),
        ("price", "Price: lowest first", RoomType.base_price, RoomType.name, RoomType.id),
        ("price_desc", "Price: highest first", RoomType.base_price.desc(), RoomType.name, RoomType.id),
    ), "name")
    statement = select(RoomType)
    if query:
        statement = statement.where(contains_any(query, RoomType.name, RoomType.code, RoomType.bed_type))
    if status in ("listed", "delisted"):
        statement = statement.where(RoomType.listed.is_(status == "listed"))
    return render_template(
        "manager/catalogue.html", section="types", page=list_page(statement, options), options=options,
        filters={"q": query, "status": status},
    )


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
    query = search_query()
    floor = request.args.get("floor", "")
    type_id = request.args.get("type", type=int)
    bed = request.args.get("bed", "")
    window = request.args.get("window", "")
    status = request.args.get("status", "")

    floor_rooms = {}
    for room_id, number in db.session.execute(select(Room.id, Room.number)):
        floor_rooms.setdefault(floor_key(number), []).append(room_id)
    if floor not in floor_rooms:
        floor = ""
    floors = [(key, floor_label(key), len(floor_rooms[key])) for key in sorted(floor_rooms, key=floor_sort)]
    room_types = db.session.scalars(select(RoomType).order_by(RoomType.name, RoomType.id)).all()
    types_by_id = {room_type.id: room_type for room_type in room_types}

    blocked = select(RoomBlock.id).where(
        RoomBlock.room_id == Room.id, RoomBlock.effective.is_(True),
        RoomBlock.start_date <= day, RoomBlock.end_date > day,
    ).exists()
    night = select(Booking.status).select_from(RoomNight).join(Booking).where(
        RoomNight.room_id == Room.id, RoomNight.stay_date == day,
        Booking.status.in_(("confirmed", "checked_in", "checked_out")),
    ).scalar_subquery()
    active_stay = select(Booking.id).where(
        Booking.room_id == Room.id, Booking.status == "checked_in", Booking.check_in <= day,
    )
    operational = (Room.operational_from <= day) & or_(Room.operational_until.is_(None), Room.operational_until > day)
    in_house = active_stay.exists() if day >= today else False
    departing = active_stay.where(Booking.check_out == day).exists() if day >= today else False
    room_status = case(
        (~operational, "out_of_service"),
        (blocked & or_(night.is_not(None), in_house), "maintenance_conflict"),
        (blocked, "maintenance"),
        (night.in_(("checked_in", "checked_out")) if day <= today else False, "occupied"),
        (departing, "departing"),
        (in_house, "occupied"),
        (night.is_not(None), "reserved"),
        (Room.housekeeping == "dirty" if day == today else False, "dirty"),
        else_="available",
    )
    numeric = Room.number.regexp_match("^[0-9]+$")
    number_value = case((numeric, cast(Room.number, Numeric(20, 0))))
    starts_with_letters = Room.number.regexp_match("^[A-Za-z]")
    number_format = case((Room.number.regexp_match("^[0-9]{3,}$"), 0), (starts_with_letters, 1), else_=2)
    # Keep numeric floors, named room prefixes, and miscellaneous numbers adjacent.
    number_order = (number_format, number_value, Room.number, Room.id)
    options = list_options((
        ("number", "Room number: ascending", *number_order),
        ("number_desc", "Room number: descending", number_format, number_value.desc(), Room.number.desc(), Room.id),
        ("type", "Room type A–Z", RoomType.name, *number_order),
        ("status", "Night status A–Z", room_status, *number_order),
        ("opening", "Operating date: newest first", Room.operational_from.desc(), *number_order),
    ), "number")
    statement = select(Room).join(RoomType)
    if floor:
        statement = statement.where(Room.id.in_(floor_rooms[floor]))
    if query:
        statement = statement.where(contains_any(query, Room.number, RoomType.name, RoomType.code))
    if type_id in types_by_id:
        statement = statement.where(Room.room_type_id == type_id)
    if bed in ("single", "double", "twin", "king", "suite"):
        statement = statement.where(RoomType.bed_type == bed)
    if window in ("with", "without"):
        statement = statement.where(Room.has_window.is_(window == "with"))
    if status in ROOM_STATUSES:
        statement = statement.where(room_status == status)
    page = list_page(statement, options)
    states = dict(db.session.execute(select(Room.id, room_status).where(
        Room.id.in_([room.id for room in page.items]),
    )).all()) if page.items else {}
    by_floor = options["sort"] in ("number", "number_desc") or bool(floor)
    groups = [
        (floor_label(key), list(items))
        for key, items in groupby(page.items, key=lambda room: floor_key(room.number))
    ] if by_floor else [("Room results", page.items)]
    return render_template(
        "manager/rooms.html", page=page, groups=groups, options=options, statuses=ROOM_STATUSES,
        states=states, floors=floors, types_by_id=types_by_id,
        filters={"q": query, "day": day, "floor": floor, "type": type_id, "status": status, "bed": bed, "window": window},
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
    query = search_query()
    status = request.args.get("status", "")
    options = list_options((
        ("active", "Active rules first", PriceRule.active.desc(), RoomType.name, PriceRule.start_date.desc(), PriceRule.id.desc()),
        ("date", "Start date: latest first", PriceRule.start_date.desc(), PriceRule.id.desc()),
        ("date_asc", "Start date: earliest first", PriceRule.start_date, PriceRule.id),
        ("amount", "Price: lowest first", PriceRule.amount, PriceRule.id),
        ("amount_desc", "Price: highest first", PriceRule.amount.desc(), PriceRule.id),
        ("type", "Room type A–Z", RoomType.name, PriceRule.id),
    ), "active")
    statement = select(PriceRule).join(RoomType)
    if query:
        statement = statement.where(contains_any(query, RoomType.name, RoomType.code, PriceRule.kind))
    if status in ("active", "retired"):
        statement = statement.where(PriceRule.active.is_(status == "active"))
    page = list_page(statement, options)
    types_by_id = dict(db.session.execute(select(RoomType.id, RoomType.name).where(
        RoomType.id.in_({rule.room_type_id for rule in page.items}),
    )).all()) if page.items else {}
    return render_template(
        "manager/catalogue.html", section="prices", page=page, options=options,
        types_by_id=types_by_id, filters={"q": query, "status": status},
    )


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
    query = search_query()
    status = request.args.get("status", "")
    options = list_options((
        ("email", "Email A–Z", User.email, User.id),
        ("email_desc", "Email Z–A", User.email.desc(), User.id),
        ("active", "Active first", User.active.desc(), User.email, User.id),
        ("inactive", "Inactive first", User.active, User.email, User.id),
    ), "email")
    statement = select(User).where(User.role == STAFF)
    if query:
        statement = statement.where(contains_any(query, User.email))
    if status in ("active", "inactive"):
        statement = statement.where(User.active.is_(status == "active"))
    return render_template(
        "manager/accounts.html", page=list_page(statement, options), options=options,
        filters={"q": query, "status": status},
    )


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
            return redirect(list_url("manager.blacklist"))
    elif form.is_submitted():
        status = 400
    else:
        form.customer_id.data = request.args.get("customer", type=int)
    search = search_query()
    customer_options = list_options((
        ("name", "Name A–Z", Customer.name, Customer.id),
        ("name_desc", "Name Z–A", Customer.name.desc(), Customer.id),
        ("new", "Customer ID: newest first", Customer.id.desc()),
    ), "name", prefix="customer_")
    history_query = search_query("history_q")
    history_status = request.args.get("history_status", "")
    history_options = list_options((
        ("recent", "Most recent first", BlacklistEntry.id.desc()),
        ("old", "Oldest first", BlacklistEntry.id),
        ("name", "Customer A–Z", Customer.name, BlacklistEntry.id.desc()),
    ), "recent", prefix="history_")
    customer_statement = select(Customer).outerjoin(User)
    if search:
        customer_statement = customer_statement.where(contains_any(search, Customer.name, Customer.phone, User.email))
    history_statement = select(BlacklistEntry).join(Customer).options(joinedload(BlacklistEntry.customer))
    if history_query:
        history_statement = history_statement.where(contains_any(history_query, Customer.name, BlacklistEntry.reason))
    if history_status in ("active", "removed"):
        history_statement = history_statement.where(
            BlacklistEntry.removed_at.is_(None) if history_status == "active" else BlacklistEntry.removed_at.is_not(None)
        )
    customers = list_page(customer_statement, customer_options)
    entries = list_page(history_statement, history_options)
    history_filters = {"history_q": history_query, "history_status": history_status}
    return render_template(
        "manager/blacklist.html", form=form, customers=customers, entries=entries,
        customer_options=customer_options, history_options=history_options,
        search=search, history_filters=history_filters,
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
    query = search_query()
    options = list_options((
        ("recent", "Newest booking first", Booking.id.desc()),
        ("arrival", "Check-in: earliest first", Booking.check_in, Booking.id),
        ("arrival_desc", "Check-in: latest first", Booking.check_in.desc(), Booking.id.desc()),
        ("customer", "Customer A–Z", Customer.name, Booking.id.desc()),
        ("room", "Room number A–Z", Room.number, Booking.id.desc()),
        ("total", "Total: highest first", Booking.total.desc(), Booking.id.desc()),
    ), "recent")
    statement = select(Booking).join(Customer).join(Room).options(lazyload(Booking.price_lines))
    if status in BOOKING_STATUSES:
        statement = statement.where(Booking.status == status)
    if query:
        matching = contains_any(query, Customer.name, Customer.phone, Room.number)
        number = query.upper().removeprefix("BK-")
        if number.isascii() and number.isdigit() and len(number) <= 10 and int(number) <= 2147483647:
            by_reference = Booking.id == int(number)
            matching = by_reference if query.upper().startswith("BK-") else or_(matching, by_reference)
        statement = statement.where(matching)
    return render_template(
        "manager/bookings.html", page=list_page(statement, options), options=options,
        filters={"q": query, "status": status}, statuses=BOOKING_STATUSES,
    )


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
    return redirect(list_url("manager.booking_detail", booking_id=booking_id))


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
    options = list_options((
        ("recent", "Most recent first", AuditLog.occurred_at.desc(), AuditLog.id.desc()),
        ("old", "Oldest first", AuditLog.occurred_at, AuditLog.id),
        ("actor", "Actor A–Z", User.email, AuditLog.id.desc()),
        ("action", "Action A–Z", AuditLog.action, AuditLog.id.desc()),
    ), "recent")
    statement = select(AuditLog).join(User).options(joinedload(AuditLog.actor))
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
    page = list_page(statement, options)
    actions = db.session.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()
    objects = db.session.scalars(select(AuditLog.object_type).distinct().order_by(AuditLog.object_type)).all()
    return render_template(
        "manager/audit.html", page=page, options=options, filters=filters, actions=actions, objects=objects,
    )
