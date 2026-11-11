from datetime import date, timedelta
from decimal import Decimal

import click
from flask.cli import with_appcontext
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from .accounts import normalize_email
from .audit import record_audit
from .catalog import hotel_now
from .extensions import db
from .models import MANAGER, PriceRule, Room, RoomType, User


@click.command("bootstrap-manager")
@click.argument("email")
@click.option("--password", envvar="BOOTSTRAP_MANAGER_PASSWORD", prompt=True,
              hide_input=True, confirmation_prompt=True)
@with_appcontext
def bootstrap_manager(email, password):
    """Create a manager from a trusted server shell."""
    email = normalize_email(email)
    if not email or "@" not in email or len(email) > 254 or not 8 <= len(password) <= 128:
        raise click.ClickException("Supply an email and an 8–128 character password.")
    with Session(db.engine) as session, session.begin():
        if session.scalar(db.select(User.id).where(User.email == email)) is not None:
            raise click.ClickException("An account with this email already exists.")
        user = User(email=email, password_hash=generate_password_hash(password), role=MANAGER)
        session.add(user)
        session.flush()
        record_audit(session, user.id, "bootstrap_manager", "user", user.id)
    click.echo("Manager account created.")


@click.command("seed-demo")
@with_appcontext
def seed_demo():
    """Add sample rooms and price rules to a development database."""
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
                code=code, name=name, description=description, capacity=capacity,
                base_price=Decimal(price), listed=True,
            )
            session.add(room_type)
            session.flush()
            session.add_all(
                Room(number=number, room_type_id=room_type.id, operational_from=date(2020, 1, 1))
                for number in numbers
            )
            session.add_all([
                PriceRule(
                    room_type_id=room_type.id, kind="season", start_date=today,
                    end_date=today + timedelta(days=7), amount=Decimal(price) + Decimal("40.00"),
                ),
                PriceRule(
                    room_type_id=room_type.id, kind="date", start_date=today + timedelta(days=2),
                    end_date=today + timedelta(days=3), amount=Decimal(price) + Decimal("90.00"),
                ),
            ])
    click.echo("Demo catalogue ready.")
