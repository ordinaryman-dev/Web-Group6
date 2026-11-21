# 员工页面的数据准备：只查询并组织筛选、房态、当班任务和抽屉内容，不提交业务写入。
import re
from datetime import date, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from flask import request, url_for
from flask_login import current_user
from sqlalchemy import func, or_, select
from wtforms import SelectChoice

from . import desk
from .booking import HOTEL_ZONE, cancellation_deadline, customer_is_blocked, hotel_now
from .extensions import db
from .floorplan import build_floor_plan, render_floor_plan
from .frontdesk_forms import (
    BlockForm, CancelBookingForm, ContactForm, DecisionForm, NoShowForm,
    PaymentForm, ReassignForm, RefundForm, WalkInForm,
)
from .models import (
    MANAGER, Booking, BookingRequest, Customer, PaymentRecord,
    Room, RoomBlock, RoomNight, RoomType, User, utc_now,
)
from .room_numbers import (
    floor_key as _floor_key, floor_label as _floor_label, floor_sort as _floor_sort,
)


TIMELINE_DAYS = 15  # today plus fourteen days
MAX_STAY_DAYS = 21
LATE_ARRIVAL_PAGE_SIZE = 5
GUEST_REQUEST_PAGE_SIZE = 3
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
    "studio": "Studio",
    "suite": "Suite",
}
ORDER_TABS = {
    "arrivals": "Arrivals",
    "inhouse": "In house",
    "departures": "Departures",
    "all": "All orders",
}


def _room_type_choices():
    rows = db.session.scalars(
        select(RoomType).where(RoomType.listed.is_(True)).order_by(RoomType.base_price)
    ).all()
    return [SelectChoice(row.id, f"{row.name} · ¥{row.base_price:,.2f}") for row in rows]


def _parse_date(value, fallback):
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


def _clamp(value, low, high):
    return max(low, min(value, high))


def _paginate(items, page, per_page):
    """Slice a list for a small sidebar pager, keeping the page in range."""
    items = list(items)
    pages = max((len(items) + per_page - 1) // per_page, 1)
    page = _clamp(page or 1, 1, pages)
    start = (page - 1) * per_page
    return items[start:start + per_page], page, pages


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


def _order_rows(session, tab, status, query, now, page=1, per_page=50):
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
    total = session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    page = max(page, 1)
    bookings = session.scalars(statement.limit(per_page).offset((page - 1) * per_page)).all()
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
    return rows, total


def desk_snapshot(session, now=None):
    # 一次组织待处理申请、逾期到店、维修冲突和脏房，供初次渲染与轮询共同使用。
    """Pending requests, late arrivals, maintenance conflicts and dirty rooms.

    The page and the polling endpoint both call this, so the bell and the counters
    can never disagree about how much is waiting for the front desk.
    """

    now = now or hotel_now()
    today = now.date()

    pending = desk.pending_requests(session)

    late_arrivals = []
    late_candidates = session.scalars(
        select(Booking)
        .where(
            Booking.status == desk.CONFIRMED,
            Booking.checked_in_at.is_(None),
            Booking.check_in <= today,
        )
        .order_by(Booking.check_in, Booking.id)
    ).all()
    for booking in late_candidates:
        deadline = desk.contact_deadline(booking)
        if now.replace(tzinfo=None) > deadline:
            late_arrivals.append({"booking": booking, "deadline": deadline})

    dirty_rooms = session.scalars(
        select(Room).where(Room.housekeeping == "dirty").order_by(Room.number)
    ).all()

    # A block never overwrites a booking, it flags the pair.
    blocks = session.scalars(
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
        affected = session.scalars(
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

    counts = {
        "requests": len(pending),
        "late": len(late_arrivals),
        "conflicts": len(conflicts),
        "dirty": len(dirty_rooms),
    }
    # Pending ids are part of the version so a new request is noticed even when the
    # total stays the same (one approved, one submitted).
    version = "|".join(
        [*(str(value) for value in counts.values()),
         *(str(row.id) for row in pending)]
    )
    return {
        "pending": pending,
        "late_arrivals": late_arrivals,
        "conflicts": conflicts,
        "dirty_rooms": dirty_rooms,
        "notifications": notifications,
        "counts": counts,
        "total": sum(counts.values()),
        "version": version,
    }


def _selection_context(now):
    # 将 URL 参数转换为受限的日期、筛选条件和选中对象，无效值回退到默认值。
    today = now.date()
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
    stay_nights = _clamp(
        request.args.get("nights", type=int) or 1,
        1,
        MAX_STAY_DAYS,
    )
    stay_end = selected_date + timedelta(days=stay_nights)
    return {
        "view": view,
        "query": query,
        "matches": matches,
        "booking_param": booking_param,
        "room_param": room_param,
        "selected_date": selected_date,
        "state_filter": state_filter,
        "bed_filter": bed_filter,
        "window_filter": window_filter,
        "type_filter": type_filter,
        "stay_on": stay_on,
        "stay_nights": stay_nights,
        "stay_end": stay_end,
    }


def _board_context(now, selection):
    # 准备完整日期区间的数据，但只绘制当前楼层；筛选只影响展示，不代表最终可分配结果。
    today = now.date()
    selected_date = selection["selected_date"]
    stay_end = selection["stay_end"]
    room_param = selection["room_param"]
    view = selection["view"]
    state_filter = selection["state_filter"]
    bed_filter = selection["bed_filter"]
    window_filter = selection["window_filter"]
    type_filter = selection["type_filter"]
    stay_on = selection["stay_on"]
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

    plan_html = None
    if view == "grid" and cards:
        # The floor plan IS the room board: same filters, same states, same drawer.
        type_by_id = {room_type.id: room_type for room_type in room_types}
        plan_state = {
            "free": "available",
            "arriving": "reserved",
            "in_house": "occupied",
            "departing": "departing",
            "dirty": "dirty",
            "blocked": "out_of_service",
        }
        layout = build_floor_plan([info["room"] for info in cards])
        plan_html = render_floor_plan(
            layout,
            {info["room"].id: plan_state.get(info["cell"]["state"], "available") for info in cards},
            type_by_id,
            lambda room: url_for("frontdesk.index", view="grid",
                                 date=selected_date.isoformat(), floor=active_floor,
                                 room=room.id),
            matched={info["room"].id for info in cards if not info["dim"]},
        )

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
    return {
        "board_by_room": board_by_room,
        "rooms": rooms,
        "room_types": room_types,
        "room_type_by_id": room_type_by_id,
        "floor_counts": floor_counts,
        "active_floor": active_floor,
        "spotlight": spotlight,
        "cards": cards,
        "state_counts": state_counts,
        "plan_html": plan_html,
        "timeline": timeline,
        "floor_tabs": floor_tabs,
    }


def _shift_context(now):
    # 组织当班统计、通知和逾期到店分页，页面与通知接口复用相同业务口径。
    today = now.date()
    arrivals_today, in_house, departures_today = desk.desk_today(db.session, now)
    snapshot = desk_snapshot(db.session, now)
    pending = snapshot["pending"]
    late_arrivals = snapshot["late_arrivals"]
    late_page_items, late_page, late_pages = _paginate(
        late_arrivals, request.args.get("late_page", type=int), LATE_ARRIVAL_PAGE_SIZE,
    )

    occupied_today = len(set(db.session.scalars(
        select(RoomNight.room_id).where(RoomNight.stay_date == today).distinct()
    ).all()))
    dirty_rooms = snapshot["dirty_rooms"]
    conflicts = snapshot["conflicts"]
    notifications = snapshot["notifications"]
    notification_total = snapshot["total"]
    return {
        "arrivals_today": arrivals_today,
        "in_house": in_house,
        "departures_today": departures_today,
        "snapshot": snapshot,
        "pending": pending,
        "late_arrivals": late_arrivals,
        "late_page_items": late_page_items,
        "late_page": late_page,
        "late_pages": late_pages,
        "occupied_today": occupied_today,
        "dirty_rooms": dirty_rooms,
        "conflicts": conflicts,
        "notifications": notifications,
        "notification_total": notification_total,
    }


def _drawer_context(now, selection, board, is_manager):
    # 根据选中订单或房间计算可见按钮和说明，点击后仍由后端业务服务再检查。
    today = now.date()
    booking_param = selection["booking_param"]
    room_param = selection["room_param"]
    selected_date = selection["selected_date"]
    board_by_room = board["board_by_room"]
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
        block_conflicts = desk.maintenance_conflicts(
            db.session, current_block.room_id, current_block.start_date, current_block.end_date,
        )
    return {
        "selected_booking": selected_booking,
        "selected_room": selected_room,
        "selected_cell": selected_cell,
        "drawer_mode": drawer_mode,
        "checks": checks,
        "paid": paid,
        "due": due,
        "payments": payments,
        "check_in_allowed": check_in_allowed,
        "check_in_reason": check_in_reason,
        "contact_due": contact_due,
        "no_show_allowed": no_show_allowed,
        "no_show_hint": no_show_hint,
        "deadline": deadline,
        "late_cancel": late_cancel,
        "checkout_late": checkout_late,
        "overdue_checkout": overdue_checkout,
        "early_checkout": early_checkout,
        "reassign_allowed": reassign_allowed,
        "can_pay": can_pay,
        "can_refund": can_refund,
        "can_cancel_directly": can_cancel_directly,
        "booking_requests": booking_requests,
        "block_conflicts": block_conflicts,
        "current_block": current_block,
    }


def _form_context(now, selection, board, drawer):
    # 准备带前缀的表单，避免一个页面中的多个表单字段互相覆盖。
    today = now.date()
    selected_date = selection["selected_date"]
    selected_room = drawer["selected_room"]
    selected_cell = drawer["selected_cell"]
    drawer_mode = drawer["drawer_mode"]
    room_type_by_id = board["room_type_by_id"]
    walk_form = WalkInForm(prefix="walk")
    walk_form.room_type_id.choices = _room_type_choices()
    if selected_room is not None:
        # A walk-in opened from a room card is tied to that room, so the type follows
        # the room rather than offering a type that the room cannot satisfy.
        chosen_type = room_type_by_id.get(selected_room.room_type_id)
        if chosen_type is not None:
            walk_form.room_type_id.choices = [SelectChoice(
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
    walk_blocked_ids = {
        customer.id for customer in walk_matches
        if customer_is_blocked(db.session, customer.id, utc_now())
    }
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


    payment_form = PaymentForm(prefix="pay")
    payment_form.idempotency_key.data = uuid4().hex
    refund_form = RefundForm(prefix="ref")
    refund_form.idempotency_key.data = uuid4().hex
    return {
        "walk_form": walk_form,
        "walk_customer": walk_customer,
        "walk_matches": walk_matches,
        "walk_blocked_ids": walk_blocked_ids,
        "block_form": block_form,
        "walk_allowed": walk_allowed,
        "walk_reason": walk_reason,
        "payment_form": payment_form,
        "refund_form": refund_form,
    }


def _desk_url(**overrides):
    args = {key: value for key, value in request.args.items()}
    for key, value in overrides.items():
        if value is None:
            args.pop(key, None)
        else:
            args[key] = value
    return url_for("frontdesk.index", **args)


def _list_context(now, selection, pending):
    # 分别计算订单、客户申请和逾期到店列表的页码，保留各自的筛选状态。
    query = selection["query"]
    view = selection["view"]
    order_tab = request.args.get("tab") or ("all" if query else "arrivals")
    if order_tab not in ORDER_TABS:
        order_tab = "all" if query else "arrivals"
    order_page = max(request.args.get("page", type=int) or 1, 1)
    if view == "orders":
        order_rows, order_total = _order_rows(
            db.session, order_tab, request.args.get("status") or None, query, now,
            page=order_page,
        )
    else:
        order_rows, order_total = [], 0
    order_pages = max((order_total + 49) // 50, 1)
    if order_page > order_pages and view == "orders":
        order_page = order_pages
        order_rows, order_total = _order_rows(
            db.session, order_tab, request.args.get("status") or None, query, now,
            page=order_page,
        )
    task_rows = _task_rows(db.session, pending, now) if view == "tasks" else []
    task_page_items, task_page, task_pages = _paginate(
        task_rows, request.args.get("request_page", type=int), GUEST_REQUEST_PAGE_SIZE,
    )
    return {
        "order_tab": order_tab,
        "order_page": order_page,
        "order_rows": order_rows,
        "order_total": order_total,
        "order_pages": order_pages,
        "task_rows": task_rows,
        "task_page_items": task_page_items,
        "task_page": task_page,
        "task_pages": task_pages,
    }


def build_desk_context(now, is_manager):
    # 按页面职责组装上下文；此处只准备显示数据，不执行提交、退款或房间分配。
    selection = _selection_context(now)
    board = _board_context(now, selection)
    shift = _shift_context(now)
    drawer = _drawer_context(now, selection, board, is_manager)
    forms = _form_context(now, selection, board, drawer)
    lists = _list_context(now, selection, shift["pending"])
    context = {**selection, **board, **shift, **drawer, **forms, **lists}
    context.update(
        now=now,
        epoch_ms=int(now.timestamp() * 1000),
        today=now.date(),
        is_manager=is_manager,
        floor_numbers=sum(key.isdigit() for key in board["floor_counts"]),
        total_rooms=len(board["rooms"]),
        selected=drawer["selected_booking"],
        selected_room_type=(board["room_type_by_id"].get(drawer["selected_room"].room_type_id)
                            if drawer["selected_room"] else None),
        close_url=_desk_url(room=None, booking=None),
        desk_url=_desk_url,
        notify_version=shift["snapshot"]["version"],
        max_stay_days=MAX_STAY_DAYS,
        bed_labels=BED_LABELS,
        state_labels=STATE_LABELS,
        order_tabs=ORDER_TABS,
        status_filter=request.args.get("status") or "",
        reassign_form=ReassignForm(prefix="re"),
        decision_form=DecisionForm(prefix="req"),
        contact_form=ContactForm(prefix="con"),
        no_show_form=NoShowForm(prefix="ns"),
        cancel_form=CancelBookingForm(prefix="cx"),
        check_in_from=desk.CHECK_IN_FROM.strftime("%H:%M"),
        check_out_by=desk.CHECK_OUT_BY.strftime("%H:%M"),
    )
    return context
