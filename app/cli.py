# 命令行维护工具：创建管理员、生成演示数据及维护演示库存；运行前按 README 选择对应命令。
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import click
from flask.cli import with_appcontext
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from .allocation import ranked_candidates
from .audit import record_audit
from .booking import HOTEL_ZONE, Stay, deadline_for, hotel_now
from .extensions import db
from .models import (
    Booking,
    BookingPriceLine,
    BookingRequest,
    Customer,
    GUEST,
    MANAGER,
    PaymentRecord,
    PriceRule,
    Room,
    RoomBlock,
    RoomNight,
    RoomType,
    STAFF,
    User,
    utc_now,
)


@click.command("create-manager")
@click.argument("email")
@click.password_option()
@with_appcontext
def create_manager(email, password):
    """Create a manager from the local application CLI."""
    email = email.strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise click.ClickException("Enter a valid email address.")
    if not 8 <= len(password) <= 128:
        raise click.ClickException("The password must contain 8 to 128 characters.")
    try:
        with Session(db.engine) as session, session.begin():
            manager = User(email=email, password_hash=generate_password_hash(password), role=MANAGER)
            session.add(manager)
            session.flush()
            record_audit(session, manager.id, "create_manager", "user", manager.id, details={"source": "local_cli"})
    except IntegrityError as exc:
        raise click.ClickException("This email already has an account.") from exc
    click.echo(f"Manager account created for {email}.")


DEMO_GUEST_EMAIL = "guest@example.test"
DEMO_GUEST_PASSWORD = "Guest2026!"


DEMO_STAFF_EMAIL = "desk@example.test"
DEMO_STAFF_PASSWORD = "Desk2026!"


@click.command("seed-desk")
@with_appcontext
def seed_desk():
    """Add a demo front desk: staff login, rooms 301-306, today's bookings."""
    today = hotel_now().date()
    with Session(db.engine) as session, session.begin():
        staff = session.scalar(db.select(User).where(User.email == DEMO_STAFF_EMAIL))
        if staff is None:
            staff = User(
                email=DEMO_STAFF_EMAIL,
                password_hash=generate_password_hash(DEMO_STAFF_PASSWORD),
                role=STAFF,
            )
            session.add(staff)
            session.flush()
            record_audit(session, staff.id, "create_staff", "user", staff.id,
                         details={"email": DEMO_STAFF_EMAIL, "source": "seed"})

        # 演示前台优先使用实体房已有的房型。只有房号不存在（例如单独执行
        # seed-desk）时才创建兜底房型，避免 seed-rooms 之后出现同名重复房型。
        room_specs = (
            ("301", "king", "harbour-king", "Harbour King", "Sea-facing king room.", 2, "680.00"),
            ("302", "king", "harbour-king", "Harbour King", "Sea-facing king room.", 2, "680.00"),
            ("305", "king", "harbour-king", "Harbour King", "Sea-facing king room.", 2, "680.00"),
            ("306", "king", "harbour-king", "Harbour King", "Sea-facing king room.", 2, "680.00"),
            ("303", "twin", "city-twin", "City Twin", "Twin beds facing the city.", 2, "520.00"),
            ("304", "twin", "city-twin", "City Twin", "Twin beds facing the city.", 2, "520.00"),
        )

        def desk_room_type(bed_type, code, name, description, capacity, price):
            # 先找已有实体房的同床型房型；再按 code 复用；最后才新建。
            occupied = db.select(Room.id).where(
                Room.room_type_id == RoomType.id
            ).correlate(RoomType).exists()
            existing = session.scalar(
                db.select(RoomType)
                .where(RoomType.bed_type == bed_type,
                       RoomType.listed.is_(True),
                       occupied)
                .order_by(RoomType.id)
            )
            if existing is not None:
                return existing
            existing = session.scalar(
                db.select(RoomType).where(RoomType.code == code)
            )
            if existing is not None:
                return existing
            room_type = RoomType(
                code=code, name=name, description=description,
                capacity=capacity, base_price=Decimal(price),
                bed_type=bed_type, listed=True,
            )
            session.add(room_type)
            session.flush()
            return room_type

        rooms = {}
        room_types = {}
        for number, bed_type, code, name, description, capacity, price in room_specs:
            room = session.scalar(db.select(Room).where(Room.number == number))
            if room is None:
                room_type = desk_room_type(
                    bed_type, code, name, description, capacity, price
                )
                room = Room(number=number, room_type_id=room_type.id,
                            operational_from=date(2020, 1, 1))
                session.add(room)
                session.flush()
            rooms[number] = room
            room_types[number] = session.get(RoomType, room.room_type_id)
        session.flush()

        # 老版本 seed-desk 曾把 301-306 的订单写成 Harbour King/City Twin，
        # 与实体房房型不一致。重复执行本命令时先按房间实际房型修正旧订单、
        # 逐晚金额和成功收款，再删除没有房间也没有订单的重复房型。
        repaired = 0
        seed_bookings = session.scalars(
            db.select(Booking).where(
                Booking.actor_user_id == staff.id,
                Booking.idempotency_key.like("seed-%"),
            )
        ).all()
        for booking in seed_bookings:
            actual_type = session.get(RoomType, booking.room.room_type_id)
            if actual_type is None:
                continue
            nights = (booking.check_out - booking.check_in).days
            total = actual_type.base_price * nights
            guests = min(booking.guests, actual_type.capacity)
            if (booking.room_type_id == actual_type.id
                    and booking.total == total
                    and booking.guests == guests):
                continue
            booking.room_type_id = actual_type.id
            booking.total = total
            booking.guests = guests
            for line in session.scalars(
                db.select(BookingPriceLine).where(
                    BookingPriceLine.booking_id == booking.id
                )
            ):
                line.amount = actual_type.base_price
                line.price_rule_id = None
            for payment in session.scalars(
                db.select(PaymentRecord).where(
                    PaymentRecord.booking_id == booking.id,
                    PaymentRecord.kind == "payment",
                    PaymentRecord.result == "success",
                )
            ):
                payment.amount = total
            repaired += 1
        session.flush()
        if repaired:
            click.echo(
                f"Repaired {repaired} demo booking(s) to match their physical room type."
            )

        for code in ("harbour-king", "city-twin"):
            room_type = session.scalar(
                db.select(RoomType).where(RoomType.code == code)
            )
            if room_type is None:
                continue
            has_room = session.scalar(
                db.select(Room.id).where(Room.room_type_id == room_type.id).limit(1)
            )
            has_booking = session.scalar(
                db.select(Booking.id).where(Booking.room_type_id == room_type.id).limit(1)
            )
            has_rule = session.scalar(
                db.select(PriceRule.id).where(PriceRule.room_type_id == room_type.id).limit(1)
            )
            if has_room is None and has_booking is None and has_rule is None:
                session.delete(room_type)
        session.flush()

        if session.scalar(db.select(Booking.id).where(Booking.actor_user_id == staff.id).limit(1)):
            click.echo("Front desk demo data is already present.")
            return

        def guest(name, phone):
            customer = Customer(name=name, phone=phone)
            session.add(customer)
            session.flush()
            return customer

        def stay(customer, room, room_type, nights, status, arrival, paid, offset_in=0):
            check_in = today + timedelta(days=offset_in)
            check_out = check_in + timedelta(days=nights)
            booking = Booking(
                customer_id=customer.id, room_id=room.id, room_type_id=room_type.id,
                actor_user_id=staff.id, idempotency_key=f"seed-{customer.id}-{room.id}-{check_in}",
                request_digest="seed", check_in=check_in, check_out=check_out,
                guests=min(2, room_type.capacity), expected_arrival=arrival,
                total=room_type.base_price * nights,
                cancel_by=deadline_for(check_in),
                status=status,
                checked_in_at=utc_now() if status != "confirmed" else None,
            )
            session.add(booking)
            session.flush()
            for night in range(nights):
                day = check_in + timedelta(days=night)
                session.add(RoomNight(room_id=room.id, stay_date=day, booking_id=booking.id))
                session.add(BookingPriceLine(booking_id=booking.id, stay_date=day,
                                             amount=room_type.base_price))
            if paid:
                session.add(PaymentRecord(
                    booking_id=booking.id, kind="payment", amount=booking.total,
                    actor_user_id=staff.id, result="success", reference=f"pay-{uuid4()}",
                ))
            return booking

        # Today's arrivals: two unpaid or partly paid, one already paid.
        a1 = stay(guest("Wei Chen", "13800000001"), rooms["301"], room_types["301"], 2,
                  "confirmed", time(14, 0), paid=False)
        a2 = stay(guest("Li Na", "13800000002"), rooms["304"], room_types["304"], 1,
                  "confirmed", time(15, 30), paid=False)
        a3 = stay(guest("Zhao Min", "13800000003"), rooms["302"], room_types["302"], 2,
                  "confirmed", time(18, 0), paid=True)

        # A late arrival: expected at 09:00 and still not here, so the desk can
        # practise the contact log and the no-show rule.
        late = stay(guest("Mark Lee", "13800000007"), rooms["306"], room_types["306"], 1,
                    "confirmed", time(9, 0), paid=False)

        # In house: arrived yesterday, leaving tomorrow.
        stay(guest("Sun Hao", "13800000004"), rooms["305"], room_types["305"], 3,
             "checked_in", time(16, 0), paid=True, offset_in=-1)

        # Departing today.
        d1 = stay(guest("Guo Jing", "13800000005"), rooms["303"], room_types["303"], 2,
                  "checked_in", time(11, 0), paid=True, offset_in=-2)

        # A maintenance block that collides with a later booking (drives the banner).
        session.add(RoomBlock(room_id=rooms["303"].id, start_date=today + timedelta(days=2),
                              end_date=today + timedelta(days=3), created_by_id=staff.id,
                              reason="Bathroom leak under repair"))
        later = stay(guest("Zhang Lei", "13800000006"), rooms["303"], room_types["303"], 2,
                     "confirmed", time(14, 30), paid=False, offset_in=2)

        # Pending guest requests for the queue.
        session.add(BookingRequest(
            booking_id=later.id, kind="cancel", target={"reason": "change of plans"},
            reason="My trip was cancelled by my office.", submitted_by=staff.id,
            status="pending",
        ))
        session.add(BookingRequest(
            booking_id=a2.id, kind="dates",
            target={"check_in": (today + timedelta(days=1)).isoformat(),
                    "check_out": (today + timedelta(days=3)).isoformat()},
            reason="Flight moved a day later.", submitted_by=staff.id, status="pending",
        ))

        click.echo(f"Front desk demo ready: {DEMO_STAFF_EMAIL} / {DEMO_STAFF_PASSWORD}")


FLOOR_ROOM_COUNT = 24
FLOOR_BED_PLAN = (
    (range(1, 7), "single"),
    (range(7, 9), "studio"),
    (range(9, 15), "double"),
    (range(15, 20), "twin"),
    (range(20, 24), "king"),
    (range(24, 25), "suite"),
)


def floor_room_number(floor, suffix):
    """301-324 and 1201-1224: the last two digits are the position on the floor."""
    return f"{floor}{suffix:02d}"


@click.command("seed-rooms")
@with_appcontext
def seed_rooms():
    """Create the 12 x 24 numbered floor inventory (293 rooms with the demo wing)."""
    with Session(db.engine) as session, session.begin():
        bed_types = {}
        for _, bed_type in FLOOR_BED_PLAN:
            if bed_type in bed_types:
                continue
            room_type = session.scalar(
                db.select(RoomType)
                .where(RoomType.bed_type == bed_type, RoomType.listed.is_(True))
                .order_by(RoomType.id)
            )
            if room_type is None:
                code, name, capacity, price = {
                    "single": ("std-single", "Classic Single", 1, "280.00"),
                    "double": ("std-double", "Comfort Double", 2, "420.00"),
                    "twin": ("std-twin", "City Twin", 2, "520.00"),
                    "king": ("std-king", "Harbour King", 2, "680.00"),
                    "studio": ("std-studio", "Harbour Studio", 2, "780.00"),
                    "suite": ("std-suite", "Family Suite", 4, "980.00"),
                }[bed_type]
                room_type = RoomType(
                    code=code,
                    name=name,
                    description=f"Standard inventory room · {bed_type} bed.",
                    capacity=capacity,
                    base_price=Decimal(price),
                    bed_type=bed_type,
                    listed=True,
                )
                session.add(room_type)
                session.flush()
            bed_types[bed_type] = room_type

        existing = set(session.scalars(db.select(Room.number)).all())
        created = 0
        for floor in range(1, 13):
            for suffix in range(1, FLOOR_ROOM_COUNT + 1):
                number = floor_room_number(floor, suffix)
                if number in existing:
                    continue
                bed_type = next(
                    bed for positions, bed in FLOOR_BED_PLAN if suffix in positions
                )
                session.add(Room(
                    number=number,
                    room_type_id=bed_types[bed_type].id,
                    operational_from=date(2020, 1, 1),
                    has_window=bed_type != "single",
                ))
                created += 1
        session.flush()
        # Warehouses built before the studio existed still hold doubles on the
        # two studio positions of each floor. Move unbooked rooms over so a
        # re-run converges on the same inventory without changing the total.
        studio = bed_types["studio"]
        double_ids = {
            room_type.id
            for room_type in session.scalars(db.select(RoomType)).all()
            if room_type.bed_type == "double" and room_type.code in ("demo-double", "std-double")
        }
        booked_room_ids = set(session.scalars(db.select(Booking.room_id)).all())
        for room in session.scalars(db.select(Room)).all():
            if not re.fullmatch(r"\d{3,4}", room.number):
                continue
            if int(room.number[-2:]) not in (7, 8):
                continue
            if room.room_type_id in double_ids and room.id not in booked_room_ids:
                room.room_type_id = studio.id
        session.flush()
        # Keep the demo inventory deterministic when the command is re-run: the
        # Classic Single rooms are the interior rooms without a window; every other
        # room type has one. This also covers the existing wing rooms.
        type_bed = {
            room_type.id: room_type.bed_type
            for room_type in session.scalars(db.select(RoomType)).all()
        }
        for room in session.scalars(db.select(Room)).all():
            room.has_window = type_bed.get(room.room_type_id) != "single"
        session.flush()
        today = hotel_now().date()
        numbered = sum(
            1 for room in session.scalars(db.select(Room)).all()
            if re.fullmatch(r"\d{3,4}", room.number)
            and (room.operational_until is None or room.operational_until > today)
        )
        click.echo(
            f"{created} room(s) created. Floors 1-12 now hold {numbered} in-service numbered rooms; "
            "the existing wing rooms were kept and window flags were refreshed."
        )
        if numbered > 12 * FLOOR_ROOM_COUNT:
            click.echo(
                "Legacy x25 rooms are still in service. "
                "Run retire-legacy-rooms --apply to bring the hotel to 293 rooms."
            )


@click.command("retire-legacy-rooms")
@click.option("--apply", is_flag=True, help="Write the changes; without this flag only show the plan.")
@with_appcontext
def retire_legacy_rooms(apply):
    """Retire the legacy 25th room on every floor and keep the inventory at 293.

    Older databases can still hold a Family Suite numbered x25 on each floor.
    Those rooms are taken out of service on the next hotel day; current and
    future stays move to a free room of the same type, and stale in-house stays
    are closed out. Booking history stays attached to the retired rooms.
    """
    today = hotel_now().date()
    with Session(db.engine) as session:
        suites = session.scalars(
            db.select(Room).join(RoomType).where(RoomType.bed_type == "suite")
        ).all()
        legacy = sorted(
            (room for room in suites if re.fullmatch(r"\d+25", room.number)),
            key=lambda room: room.number,
        )
        if not legacy:
            click.echo("No legacy x25 rooms found; the inventory is already 293.")
            return
        legacy_ids = {room.id for room in legacy}
        needs_retire = [
            room for room in legacy
            if room.operational_until is None or room.operational_until > today
        ]
        active = session.scalars(
            db.select(Booking).where(
                Booking.room_id.in_(legacy_ids),
                Booking.status.in_(("confirmed", "checked_in")),
                Booking.check_out >= today,
            ).order_by(Booking.check_in, Booking.id)
        ).all()
        stale = session.scalars(
            db.select(Booking).where(
                Booking.room_id.in_(legacy_ids),
                Booking.status == "checked_in",
                Booking.check_out < today,
            ).order_by(Booking.check_out, Booking.id)
        ).all()
        if not needs_retire and not active and not stale:
            click.echo("Legacy x25 rooms are already retired; the inventory is 293.")
            return

        moves = []
        for booking in active:
            stay = Stay(booking.check_in, booking.check_out, booking.guests)
            candidates = ranked_candidates(
                session, booking.room_type_id, stay, today,
                ignore_booking_id=booking.id, exclude_room_ids=legacy_ids,
            )
            if not candidates:
                raise click.ClickException(
                    f"No free room of the same type for {booking.reference}; nothing was changed."
                )
            target = session.get(Room, candidates[0].room_id)
            # Move as we plan so the next booking cannot be sent to the same
            # room for overlapping nights. A dry run rolls all of this back.
            session.execute(
                update(RoomNight)
                .where(RoomNight.booking_id == booking.id)
                .values(room_id=target.id)
            )
            old_number = booking.room.number
            booking.room_id = target.id
            moves.append((booking, old_number, target))

        click.echo(f"{len(legacy)} legacy room(s): {', '.join(room.number for room in legacy)}")
        for booking, old_number, target in moves:
            click.echo(
                f"  {booking.reference}: {old_number} -> {target.number} "
                f"({booking.check_in} to {booking.check_out})"
            )
        for booking in stale:
            click.echo(f"  {booking.reference}: close stale in-house stay in {booking.room.number}")

        if not apply:
            session.rollback()
            click.echo("Dry run only. Re-run with --apply to write these changes.")
            return

        for booking in stale:
            booking.status = "checked_out"
            booking.checked_out_at = datetime.combine(
                booking.check_out, time(12, 0), tzinfo=HOTEL_ZONE
            ).astimezone(timezone.utc).replace(tzinfo=None)
        for room in needs_retire:
            room.operational_until = today
        session.commit()
        click.echo("Legacy rooms retired. Floors 1-12 keep 24 rooms each; with the 5-room wing the hotel has 293.")


@click.command("seed-demo")
@with_appcontext
def seed_demo():
    """Add sample rooms, prices and a demo guest to a development database."""
    today = hotel_now().date()
    demo_types = [
        ("demo-single", "Classic Single", "A quiet room for a short stay.", 1, "280.00", "single", ["D101", "D102"]),
        ("demo-double", "Comfort Double", "A double room for two guests.", 2, "420.00", "double", ["D201", "D202"]),
        ("demo-family", "Family Suite", "A suite with space for a small family.", 4, "680.00", "suite", ["D301"]),
        ("demo-studio", "Harbour Studio", "An open-plan studio with a king bed, a lounge corner and harbour-facing windows.", 2, "780.00", "studio", []),
    ]
    with Session(db.engine) as session, session.begin():
        for code, name, description, capacity, price, bed_type, numbers in demo_types:
            room_type = session.scalar(db.select(RoomType).where(RoomType.code == code))
            if room_type is not None:
                continue
            room_type = RoomType(
                code=code,
                name=name,
                description=description,
                capacity=capacity,
                base_price=Decimal(price),
                bed_type=bed_type,
                listed=True,
            )
            session.add(room_type)
            session.flush()
            session.add_all(
                Room(number=number, room_type_id=room_type.id, operational_from=date(2020, 1, 1))
                for number in numbers
            )
            session.add_all([
                PriceRule(
                    room_type_id=room_type.id,
                    kind="season",
                    start_date=today,
                    end_date=today + timedelta(days=7),
                    amount=Decimal(price) + Decimal("40.00"),
                ),
                PriceRule(
                    room_type_id=room_type.id,
                    kind="date",
                    start_date=today + timedelta(days=2),
                    end_date=today + timedelta(days=3),
                    amount=Decimal(price) + Decimal("90.00"),
                ),
            ])

        guest = session.scalar(db.select(User).where(User.email == DEMO_GUEST_EMAIL))
        if guest is None:
            guest = User(
                email=DEMO_GUEST_EMAIL,
                password_hash=generate_password_hash(DEMO_GUEST_PASSWORD),
                role=GUEST,
            )
            session.add(guest)
            session.flush()
            session.add(Customer(user=guest, name="Demo Guest", phone="13800000000"))
        elif session.scalar(db.select(Customer).where(Customer.user_id == guest.id)) is None:
            session.add(Customer(user=guest, name="Demo Guest", phone="13800000000"))

    click.echo("Demo catalogue and guest account are ready.")

