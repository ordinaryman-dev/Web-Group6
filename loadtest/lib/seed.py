# 测试数据准备：创建虚拟用户、冲突夹具房型与唯一一间房，并可清理上一轮压测产生的订单。
"""Database fixtures used only by the HTTP test harness."""

from datetime import date

import pymysql
from werkzeug.security import generate_password_hash

from ..config import (
    CONFLICT_ROOM_NUMBER,
    CONFLICT_ROOM_TYPE_CODE,
    CONFLICT_ROOM_TYPE_NAME,
    GUEST_PASSWORD,
    MANAGER_EMAIL,
    MANAGER_PASSWORD,
    database_settings,
)
from ..lib.http import GuestClient


# 测试账号只用于登录流程，使用较低迭代次数以免把压测时间耗在密码哈希上。
TEST_HASH_METHOD = "pbkdf2:sha256:1000"
LOAD_EMAIL_PATTERN = "%@load.test"


def connect():
    return pymysql.connect(
        **database_settings(),
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def ensure_guest_accounts(count, prefix="vu"):
    """Create (or reuse) ``count`` guest accounts and return their credentials."""

    accounts = []
    with connect() as connection:
        with connection.cursor() as cursor:
            for index in range(1, count + 1):
                email = f"{prefix}{index:03d}@load.test"
                cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
                row = cursor.fetchone()
                if row is None:
                    password_hash = generate_password_hash(
                        GUEST_PASSWORD, method=TEST_HASH_METHOD
                    )
                    cursor.execute(
                        "INSERT INTO users (email, password_hash, role, active) "
                        "VALUES (%s, %s, 'guest', 1)",
                        (email, password_hash),
                    )
                    user_id = cursor.lastrowid
                    cursor.execute(
                        "INSERT INTO customers (user_id, name, phone) VALUES (%s, %s, %s)",
                        (user_id, f"Load VU {index:03d}", f"139{index:08d}"),
                    )
                else:
                    user_id = row["id"]
                accounts.append({"email": email, "password": GUEST_PASSWORD, "user_id": user_id})
        connection.commit()
    return accounts


def ensure_manager_account():
    """Create a manager that the screenshot pass can use."""

    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE email = %s", (MANAGER_EMAIL,))
            if cursor.fetchone() is None:
                cursor.execute(
                    "INSERT INTO users (email, password_hash, role, active) "
                    "VALUES (%s, %s, 'manager', 1)",
                    (
                        MANAGER_EMAIL,
                        generate_password_hash(MANAGER_PASSWORD, method=TEST_HASH_METHOD),
                    ),
                )
        connection.commit()
    return {"email": MANAGER_EMAIL, "password": MANAGER_PASSWORD}


def ensure_conflict_fixture():
    """Return the dedicated room type/room used by the same-room contention test."""

    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM room_types WHERE code = %s", (CONFLICT_ROOM_TYPE_CODE,)
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "INSERT INTO room_types "
                    "(code, name, description, capacity, base_price, listed, bed_type) "
                    "VALUES (%s, %s, %s, 1, 300.00, 1, 'single')",
                    (CONFLICT_ROOM_TYPE_CODE, CONFLICT_ROOM_TYPE_NAME,
                     "One-room inventory for HTTP contention tests."),
                )
                room_type_id = cursor.lastrowid
            else:
                room_type_id = row["id"]
            cursor.execute(
                "SELECT id FROM rooms WHERE number = %s", (CONFLICT_ROOM_NUMBER,)
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "INSERT INTO rooms "
                    "(number, room_type_id, operational_from, housekeeping, has_window) "
                    "VALUES (%s, %s, %s, 'clean', 1)",
                    (CONFLICT_ROOM_NUMBER, room_type_id, date(2020, 1, 1)),
                )
                room_id = cursor.lastrowid
            else:
                room_id = row["id"]
                cursor.execute(
                    "UPDATE rooms SET room_type_id = %s, housekeeping = 'clean', "
                    "operational_until = NULL, last_allocated_at = NULL WHERE id = %s",
                    (room_type_id, room_id),
                )
        connection.commit()
    return {"room_type_id": room_type_id, "room_id": room_id,
            "room_number": CONFLICT_ROOM_NUMBER}


def _delete_bookings(cursor, where_sql, params):
    """Delete every row that hangs off matching bookings, children first."""

    booking_filter = (
        f"JOIN bookings b ON {{table}}.booking_id = b.id {where_sql}"
    )
    for table in ("payment_records", "booking_requests", "booking_price_lines", "room_nights"):
        cursor.execute(
            f"DELETE {table} FROM {table} {booking_filter.format(table=table)}",
            params,
        )
    cursor.execute(f"DELETE b FROM bookings b {where_sql}", params)


def reset_load_bookings():
    """Remove bookings created by previous load runs so a re-run starts clean."""

    with connect() as connection:
        with connection.cursor() as cursor:
            _delete_bookings(
                cursor,
                "JOIN users u ON b.actor_user_id = u.id WHERE u.email LIKE %s",
                (LOAD_EMAIL_PATTERN,),
            )
            cursor.execute(
                "SELECT id FROM room_types WHERE code = %s", (CONFLICT_ROOM_TYPE_CODE,)
            )
            row = cursor.fetchone()
            if row:
                _delete_bookings(
                    cursor,
                    "WHERE b.room_type_id = %s",
                    (row["id"],),
                )
                cursor.execute(
                    "DELETE FROM room_blocks WHERE room_id = "
                    "(SELECT id FROM rooms WHERE number = %s)",
                    (CONFLICT_ROOM_NUMBER,),
                )
                cursor.execute(
                    "UPDATE rooms SET housekeeping = 'clean', operational_until = NULL, "
                    "last_allocated_at = NULL WHERE number = %s",
                    (CONFLICT_ROOM_NUMBER,),
                )
        connection.commit()


def remove_conflict_fixture():
    """Drop the one-room fixture after the contention suite, so demo screenshots stay at 293 rooms."""

    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM room_types WHERE code = %s", (CONFLICT_ROOM_TYPE_CODE,)
            )
            row = cursor.fetchone()
            if not row:
                return
            room_type_id = row["id"]
            _delete_bookings(cursor, "WHERE b.room_type_id = %s", (room_type_id,))
            cursor.execute("DELETE FROM room_blocks WHERE room_id IN "
                           "(SELECT id FROM rooms WHERE room_type_id = %s)", (room_type_id,))
            cursor.execute("DELETE FROM rooms WHERE room_type_id = %s", (room_type_id,))
            cursor.execute("DELETE FROM room_types WHERE id = %s", (room_type_id,))
        connection.commit()


def login_clients(count, base_url, prefix="vu"):
    """Create the accounts and return logged-in :class:`GuestClient` objects."""

    accounts = ensure_guest_accounts(count, prefix=prefix)
    clients = [
        GuestClient(account["email"], account["password"], base_url)
        for account in accounts
    ]
    for client in clients:
        client.login()
    return clients
