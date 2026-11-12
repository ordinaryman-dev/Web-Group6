import os
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from app import create_app
from app.extensions import db


@pytest.fixture(scope="session")
def app():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated empty MySQL database.")
    if make_url(url).get_backend_name() != "mysql":
        pytest.fail("These integration tests require MySQL.")
    application = create_app({
        "TESTING": True,
        "SECRET_KEY": secrets.token_hex(32),
        "SQLALCHEMY_DATABASE_URI": url,
        "HOTEL_CLOCK": lambda: datetime(2030, 6, 18, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    })
    with application.app_context():
        if inspect(db.engine).get_table_names():
            pytest.fail("TEST_DATABASE_URL must point to an empty dedicated database.")
        upgrade(revision="20261112_01")
        with db.engine.begin() as connection:
            legacy = connection.execute(text(
                "INSERT INTO room_types (code, name, description, capacity, base_price, listed) "
                "VALUES ('legacy-type', 'Legacy room type', 'Previous revision', 2, 190.00, 1)"
            ))
            connection.execute(text(
                "INSERT INTO price_rules (room_type_id, kind, start_date, end_date, amount) "
                "VALUES (:type_id, 'season', '2028-06-01', '2028-07-01', 205.00)"
            ), {"type_id": legacy.lastrowid})
        upgrade()
    return application
