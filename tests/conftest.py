import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from flask_migrate import upgrade
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import create_app
from app.extensions import db


@pytest.fixture(scope="session")
def mysql_server():
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.fail("Set TEST_DATABASE_URL to a dedicated MySQL 8 test server.", pytrace=False)
    url = make_url(raw_url)
    if url.get_backend_name() != "mysql":
        pytest.fail("The integration suite requires MySQL 8 with InnoDB.", pytrace=False)
    server_url = url.set(database=None)
    server = create_engine(server_url)
    try:
        with server.connect() as conn:
            version, engine = conn.execute(text("SELECT VERSION(), @@default_storage_engine")).one()
            assert version.startswith("8.") and engine.lower() == "innodb"
        yield url, server
    finally:
        server.dispose()


@pytest.fixture
def app(mysql_server):
    url, server = mysql_server
    database = "hero_test_" + uuid4().hex
    with server.begin() as conn:
        conn.execute(text(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4"))
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "isolated-integration-test-secret",
        "SQLALCHEMY_DATABASE_URI": url.set(database=database),
        "HOTEL_CLOCK": lambda: datetime(2026, 11, 11, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    })
    try:
        with app.app_context():
            upgrade(directory=str(Path(__file__).resolve().parents[1] / "migrations"))
        yield app
    finally:
        with app.app_context():
            db.engine.dispose()
        with server.begin() as conn:
            conn.execute(text(f"DROP DATABASE `{database}`"))


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def demo(app):
    result = app.test_cli_runner().invoke(args=["seed-demo"])
    assert result.exit_code == 0, result.output
    return app
