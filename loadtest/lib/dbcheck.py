# 压测后的一致性核验：用独立 SQL 检查超售、丢房晚、金额不一致、封锁冲突和幂等重复。
"""Post-run consistency checks executed directly against MySQL.

Each check returns ``{id, title, sql, expected, actual, passed}``. A non-zero
``actual`` for a "must be zero" check is a failed invariant.
"""

from ..config import database_settings
import pymysql


CHECKS = [
    {
        "id": "I1",
        "title": "同一房间同一晚出现两条房晚记录（超售）",
        "sql": """
            SELECT COUNT(*) AS n FROM (
                SELECT room_id, stay_date FROM room_nights
                GROUP BY room_id, stay_date HAVING COUNT(*) > 1
            ) duplicates
        """,
    },
    {
        "id": "I2",
        "title": "同一房间存在时间段重叠的有效订单",
        "sql": """
            SELECT COUNT(*) AS n
            FROM bookings a
            JOIN bookings b ON a.room_id = b.room_id AND a.id < b.id
            WHERE a.status IN ('confirmed', 'checked_in')
              AND b.status IN ('confirmed', 'checked_in')
              AND a.check_in < b.check_out AND b.check_in < a.check_out
        """,
    },
    {
        "id": "I3",
        "title": "有效订单的房晚数与住宿天数不一致",
        "sql": """
            SELECT COUNT(*) AS n FROM (
                SELECT b.id
                FROM bookings b
                LEFT JOIN room_nights rn ON rn.booking_id = b.id
                WHERE b.status IN ('confirmed', 'checked_in')
                GROUP BY b.id, b.check_in, b.check_out
                HAVING COUNT(rn.id) <> DATEDIFF(b.check_out, b.check_in)
            ) mismatched
        """,
    },
    {
        "id": "I4",
        "title": "房晚落在订单入住区间之外",
        "sql": """
            SELECT COUNT(*) AS n
            FROM room_nights rn JOIN bookings b ON rn.booking_id = b.id
            WHERE rn.stay_date < b.check_in OR rn.stay_date >= b.check_out
        """,
    },
    {
        "id": "I5",
        "title": "已取消/未到店订单仍占用房晚",
        "sql": """
            SELECT COUNT(*) AS n
            FROM room_nights rn JOIN bookings b ON rn.booking_id = b.id
            WHERE b.status NOT IN ('confirmed', 'checked_in')
        """,
    },
    {
        "id": "I6",
        "title": "有效订单的逐晚金额合计与订单总额不一致",
        "sql": """
            SELECT COUNT(*) AS n FROM (
                SELECT b.id
                FROM bookings b
                LEFT JOIN booking_price_lines pl ON pl.booking_id = b.id
                WHERE b.status IN ('confirmed', 'checked_in')
                GROUP BY b.id, b.check_in, b.check_out, b.total
                HAVING COUNT(pl.id) <> DATEDIFF(b.check_out, b.check_in)
                    OR COALESCE(SUM(pl.amount), 0) <> b.total
            ) mismatched
        """,
    },
    {
        "id": "I7",
        "title": "压测创建的订单落在维修封锁区间内",
        "sql": """
            SELECT COUNT(*) AS n
            FROM room_nights rn
            JOIN bookings b ON b.id = rn.booking_id
            JOIN users u ON u.id = b.actor_user_id
            JOIN room_blocks rb ON rb.room_id = rn.room_id AND rb.effective = 1
            WHERE rn.stay_date >= rb.start_date AND rn.stay_date < rb.end_date
              AND u.email LIKE '%@load.test'
        """,
    },
    {
        "id": "I8",
        "title": "同一操作人同一幂等键产生多张订单",
        "sql": """
            SELECT COUNT(*) AS n FROM (
                SELECT actor_user_id, idempotency_key FROM bookings
                GROUP BY actor_user_id, idempotency_key HAVING COUNT(*) > 1
            ) duplicates
        """,
    },
    {
        "id": "I9",
        "title": "同一操作人同一幂等键产生多条收款/退款",
        "sql": """
            SELECT COUNT(*) AS n FROM (
                SELECT actor_user_id, idempotency_key FROM payment_records
                WHERE idempotency_key IS NOT NULL
                GROUP BY actor_user_id, idempotency_key HAVING COUNT(*) > 1
            ) duplicates
        """,
    },
    {
        "id": "I10",
        "title": "单张订单退款金额超过成功收款金额",
        "sql": """
            SELECT COUNT(*) AS n FROM (
                SELECT booking_id
                FROM payment_records
                WHERE result = 'success'
                GROUP BY booking_id
                HAVING SUM(CASE WHEN kind = 'refund' THEN amount ELSE 0 END)
                     > SUM(CASE WHEN kind = 'payment' THEN amount ELSE 0 END)
            ) overpaid
        """,
    },
    {
        "id": "I11",
        "title": "订单总额为负，或逐晚金额为负",
        "sql": """
            SELECT
                (SELECT COUNT(*) FROM bookings WHERE total < 0)
              + (SELECT COUNT(*) FROM booking_price_lines WHERE amount < 0) AS n
        """,
    },
    {
        "id": "I12",
        "title": "订单的房晚房间与订单房型不一致",
        "sql": """
            SELECT COUNT(*) AS n
            FROM room_nights rn
            JOIN bookings b ON b.id = rn.booking_id
            JOIN rooms r ON r.id = rn.room_id
            WHERE r.room_type_id <> b.room_type_id
        """,
    },
    {
        "id": "I13",
        "title": "房晚落在房间停用期内",
        "sql": """
            SELECT COUNT(*) AS n
            FROM room_nights rn JOIN rooms r ON r.id = rn.room_id
            WHERE rn.stay_date < r.operational_from
               OR (r.operational_until IS NOT NULL AND rn.stay_date >= r.operational_until)
        """,
    },
    {
        "id": "I14",
        "title": "孤儿房晚（订单或房间已不存在）",
        "sql": """
            SELECT COUNT(*) AS n
            FROM room_nights rn
            LEFT JOIN bookings b ON b.id = rn.booking_id
            LEFT JOIN rooms r ON r.id = rn.room_id
            WHERE b.id IS NULL OR r.id IS NULL
        """,
    },
    {
        "id": "I15",
        "title": "同名房型重复（演示数据会产生重复筛选行）",
        "sql": """
            SELECT COUNT(*) AS n FROM (
                SELECT name FROM room_types GROUP BY name HAVING COUNT(*) > 1
            ) duplicates
        """,
    },
]


def run_checks(include_payment_checks=True):
    checks = CHECKS if include_payment_checks else [
        check for check in CHECKS if check["id"] not in ("I9", "I10")
    ]
    results = []
    with pymysql.connect(
        **database_settings(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            for check in checks:
                cursor.execute(check["sql"])
                actual = int(cursor.fetchone()["n"])
                results.append({
                    "id": check["id"],
                    "title": check["title"],
                    "sql": " ".join(check["sql"].split()),
                    "expected": 0,
                    "actual": actual,
                    "passed": actual == 0,
                })
    return results


def server_counters():
    """Snapshot InnoDB/connection counters for the contention section."""

    names = (
        "Questions", "Com_select", "Com_insert", "Com_update", "Com_delete",
        "Innodb_row_lock_waits", "Innodb_row_lock_time", "Innodb_row_lock_time_avg",
        "Innodb_row_lock_current_waits", "Threads_connected", "Threads_running",
        "Slow_queries", "Aborted_connects",
    )
    counters = {}
    with pymysql.connect(
        **database_settings(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(names))
            cursor.execute(
                f"SHOW GLOBAL STATUS WHERE Variable_name IN ({placeholders})", names
            )
            for row in cursor.fetchall():
                try:
                    counters[row["Variable_name"]] = int(row["Value"])
                except (TypeError, ValueError):
                    counters[row["Variable_name"]] = row["Value"]
    return counters


def counter_delta(before, after):
    keys = (
        "Questions", "Com_select", "Com_insert", "Com_update", "Com_delete",
        "Innodb_row_lock_waits", "Innodb_row_lock_time",
        "Slow_queries", "Aborted_connects",
    )
    return {
        key: int(after.get(key) or 0) - int(before.get(key) or 0)
        for key in keys
    }
