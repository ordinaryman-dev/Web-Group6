# 测试环境参数：被测地址、专用数据库、虚拟用户账号与冲突夹具房间；全部可用环境变量覆盖。
"""Shared settings for the HTTP load and conflict tests."""

import os
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo


BASE_URL = os.environ.get("LOAD_BASE_URL", "http://127.0.0.1:5001").rstrip("/")
DATABASE_URL = os.environ.get(
    "LOAD_DATABASE_URL",
    "mysql+pymysql://hero_app:HeroApp2026@127.0.0.1:3306/hero_test_20261116"
    "?charset=utf8mb4",
)

GUEST_PASSWORD = os.environ.get("LOAD_GUEST_PASSWORD", "Load2026!")
MANAGER_EMAIL = os.environ.get("LOAD_MANAGER_EMAIL", "manager@load.test")
MANAGER_PASSWORD = os.environ.get("LOAD_MANAGER_PASSWORD", "Manager2026!")

# 为“同一间房多人抢”准备一个只有一间房的专用房型，避免真实库存把并发摊薄。
CONFLICT_ROOM_TYPE_CODE = "lt-conflict"
CONFLICT_ROOM_TYPE_NAME = "Load Test Single"
CONFLICT_ROOM_NUMBER = "LT901"

HOTEL_ZONE = ZoneInfo("Asia/Shanghai")


def hotel_today():
    """Use the same Beijing calendar day as the application's business clock."""

    return datetime.now(HOTEL_ZONE).date()


def in_days(offset):
    return hotel_today() + timedelta(days=offset)


def database_settings():
    """Return PyMySQL connection arguments parsed from DATABASE_URL."""

    url = urlparse(DATABASE_URL.replace("mysql+pymysql://", "mysql://", 1))
    return {
        "host": url.hostname or "127.0.0.1",
        "port": url.port or 3306,
        "user": unquote(url.username or "root"),
        "password": unquote(url.password or ""),
        "database": (url.path or "/").lstrip("/"),
        "charset": "utf8mb4",
    }


def day_range(start_offset, nights=1):
    start = in_days(start_offset)
    return start, start + timedelta(days=nights)


def iso(day):
    return day.isoformat()
