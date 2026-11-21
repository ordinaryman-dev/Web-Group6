# 员工 HTTP 入口：读取和校验表单，调用 desk 处理业务，再返回页面；所有写入统一经过 _run。
"""The front-desk HTTP routes; business writes go through desk and _run."""

from functools import wraps
from time import sleep
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import allocation, desk
from .account_forms import PasswordForm
from .booking import BookingRejected, Stay, cancel_confirmed_booking, cancellation_deadline, hotel_now
from .extensions import db
from .frontdesk_forms import (
    BlockForm, CancelBookingForm, ContactForm, DecisionForm, NoShowForm,
    PaymentForm, ReassignForm, RefundForm, WalkInForm,
)
from .frontdesk_views import _parse_date, _room_type_choices, build_desk_context, desk_snapshot
from .models import MANAGER, STAFF, Booking, BookingRequest, Room, RoomBlock, User


frontdesk = Blueprint("frontdesk", __name__)

def desk_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role not in (STAFF, MANAGER):
            abort(403, description="The front desk is for hotel staff.")
        return view(*args, **kwargs)

    return wrapped


def _run(action, success, *, change_own_password=False):
    # 事务块自然结束才提交；异常先回滚再处理。1213/1205 等竞争错误最多重试三次，每次换新 Session。
    """Retry only rolled-back attempts; every attempt owns a fresh Session."""
    engine = db.engine.execution_options(isolation_level="READ COMMITTED")
    for attempt in range(3):
        try:
            with Session(engine) as session, session.begin():
                actor = session.get(User, current_user.id, with_for_update={"read": not change_own_password})
                if actor is None or not actor.active or actor.role not in (STAFF, MANAGER):
                    abort(403)
                action(session)
        except desk.DeskError as exc:
            flash(str(exc), "error")
            return False
        except allocation.AllocationRetry:
            pass
        except IntegrityError as exc:
            if not exc.orig.args or exc.orig.args[0] != 1062 or "uq_room_night" not in str(exc.orig):
                flash("That change collides with another record. Nothing was saved.", "error")
                return False
        except OperationalError as exc:
            if not exc.orig.args or exc.orig.args[0] not in (1205, 1213):
                raise
        else:
            flash(success() if callable(success) else success, "success")
            return True
        if attempt < 2:
            sleep(0.02 * (attempt + 1))
    flash("The rooms are busy. Please try the action again.", "error")
    return False


def _back(**overrides):
    # 只保留本站工作台路径中的查询参数，防止表单把用户跳转到外部网站。
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


def _booking_or_404(booking_id):
    booking = db.session.get(Booking, booking_id)
    if booking is None:
        abort(404)
    return booking


@frontdesk.get("/frontdesk/notifications.json")
@desk_required
def notifications_feed():
    # 页面和轮询共用同一个快照，避免通知数字与任务列表采用不同口径。
    """What the bell shows, as JSON, so the page can refresh without a reload."""

    snapshot = desk_snapshot(db.session)
    return jsonify(
        version=snapshot["version"],
        total=snapshot["total"],
        counts=snapshot["counts"],
        items=[
            {
                "kind": item["kind"],
                "tone": item["tone"],
                "title": item["title"],
                "text": item["text"],
                "href": url_for(
                    "frontdesk.index",
                    view=("grid" if item["kind"] == "conflict" else "tasks"),
                ),
            }
            for item in snapshot["notifications"]
        ],
        pending=[
            {
                "id": row.id,
                "kind": row.kind,
                "booking": row.booking.reference,
                "submitted_at": row.submitted_at.isoformat(timespec="minutes"),
            }
            for row in snapshot["pending"]
        ],
    )


@frontdesk.get("/frontdesk")
@desk_required
def index():
    # 路由只负责调用页面数据组装器并渲染模板，筛选和抽屉数据放在 frontdesk_views。
    context = build_desk_context(hotel_now(), current_user.role == MANAGER)
    return render_template("frontdesk/index.html", **context)


@frontdesk.route("/frontdesk/profile", methods=["GET", "POST"])
@desk_required
def profile():
    # 只把当前登录用户的 ID 交给服务层，不能靠表单指定另一个人的账号。
    form = PasswordForm()
    if form.validate_on_submit():
        def action(session):
            desk.change_own_password(session, current_user.id, form.current_password.data, form.new_password.data)

        if _run(action, "Password updated.", change_own_password=True):
            return redirect(url_for("frontdesk.profile"))
        status = 400
    else:
        status = 400 if form.is_submitted() else 200
    return render_template("frontdesk/profile.html", form=form), status


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/pay")
@desk_required
def pay(booking_id):
    booking = _booking_or_404(booking_id)
    form = PaymentForm(prefix="pay")
    if not form.validate_on_submit():
        flash("Check the payment amount and try again.", "error")
        return _back(booking=booking_id)
    key = form.idempotency_key.data or str(uuid4())
    amount = form.amount.data

    def action(session):
        desk.record_payment(session, session.get(Booking, booking_id), current_user.id, amount,
                            idempotency_key=key)

    _run(action, f"Payment recorded for {booking.reference}.")
    return _back(booking=booking_id)


@frontdesk.post("/frontdesk/bookings/<int:booking_id>/refund")
@desk_required
def refund(booking_id):
    booking = _booking_or_404(booking_id)
    form = RefundForm(prefix="ref")
    if not form.validate_on_submit():
        flash("Check the refund amount and try again.", "error")
        return _back(booking=booking_id)
    key = form.idempotency_key.data or str(uuid4())
    amount = form.amount.data

    def action(session):
        desk.record_refund(session, session.get(Booking, booking_id), current_user.id, amount,
                           idempotency_key=key)

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

    succeeded = _run(action, f"{booking.reference} checked out. The room is now marked dirty.")
    if succeeded and outcome.get("overdue"):
        flash("Recorded after the planned departure date.", "info")
    if succeeded and outcome.get("late"):
        flash("Recorded after 12:00, so this is a late check-out.", "info")
    if succeeded and outcome.get("early"):
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
    refund = {}

    def action(session):
        try:
            amount = cancel_confirmed_booking(
                session, current_user.id, booking_id, source, form.reason.data, now,
            )
        except BookingRejected as exc:
            raise desk.DeskError(str(exc)) from exc
        refund["amount"] = amount

    if _run(action, f"{booking.reference} cancelled by manager authority."):
        flash(f"Simulated refund: ¥{refund['amount']:,.2f}.", "info")
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
    # 已有手机号需要人工选定原客户；异常仅带普通字段传到事务外，不访问已回滚的 ORM 对象。
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
    existing_guest = {}

    def action(session):
        try:
            booking = desk.create_walkin(
                session, current_user.id, form.room_type_id.data, stay,
                customer_id=form.customer_id.data,
                name=form.name.data, phone=form.phone.data, note=form.note.data,
                room_id=form.room_id.data,
            )
        except desk.ExistingCustomerRequired as error:
            existing_guest["phone"] = error.phone
            raise
        booking_id["id"] = booking.id

    succeeded = _run(action, "Walk-in booked, paid and checked in.")
    if not succeeded and existing_guest:
        return _back(room=form.room_id.data, booking=None, lookup=existing_guest["phone"], walk_customer=None)
    return _back(booking=booking_id.get("id") if succeeded else None, room=None)


@frontdesk.get("/frontdesk/blocks/preview")
@desk_required
def block_preview():
    # 预检只提供提示；真正保存维修时，服务层仍需加锁并重新检查冲突。
    """Live conflict count for the block form, before the desk submits it."""
    room_id = request.args.get("room_id", type=int)
    start = _parse_date(request.args.get("start_date"), None)
    end = _parse_date(request.args.get("end_date"), None)
    if not room_id or start is None or end is None or end <= start:
        return jsonify({"error": "Choose a valid date range."}), 400
    conflicts = desk.maintenance_conflicts(db.session, room_id, start, end)
    return jsonify({
        "count": len(conflicts),
        "bookings": [
            {"reference": booking.reference,
             "guest": booking.customer.name,
             "check_in": booking.check_in.isoformat(),
             "check_out": booking.check_out.isoformat()}
            for booking in conflicts
        ],
    })


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
        conflicts[:] = found

    succeeded = _run(action, "Out-of-service block saved.")
    if succeeded and conflicts:
        flash(f"{len(conflicts)} booking(s) now conflict. Review them; in-house guests need manager coordination.", "info")
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
    now = hotel_now()

    def action(session):
        desk.approve_request(session, request_id, current_user.id, comment,
                             actor_role=current_user.role, today=now.date(), now=now)

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
