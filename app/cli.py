from datetime import date, timedelta
from decimal import Decimal

import click
from flask.cli import with_appcontext
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from .booking import hotel_now
from .extensions import db
from .models import Customer, GUEST, PriceRule, Room, RoomType, User


DEMO_GUEST_EMAIL = "guest@example.test"
DEMO_GUEST_PASSWORD = "Guest2026!"


@click.command("seed-demo")
@with_appcontext
def seed_demo():
    """Add sample rooms, prices and a demo guest to a development database."""
    today = hotel_now().date()
    demo_types = [
        ("demo-single", "Classic Single", "A quiet room for a short stay.", 1, "280.00", ["D101", "D102"]),
        ("demo-double", "Comfort Double", "A double room for two guests.", 2, "420.00", ["D201", "D202"]),
        ("demo-family", "Family Suite", "A suite with space for a small family.", 4, "680.00", ["D301"]),
    ]
    with Session(db.engine) as session, session.begin():
        for code, name, description, capacity, price, numbers in demo_types:
            room_type = session.scalar(db.select(RoomType).where(RoomType.code == code))
            if room_type is not None:
                continue
            room_type = RoomType(
                code=code,
                name=name,
                description=description,
                capacity=capacity,
                base_price=Decimal(price),
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
