# F1 回归检查：验证 seed-demo → seed-rooms → seed-desk 之后，演示订单与实体房型、金额、人数完全一致。
"""Check the demo seed data after the seed-desk fix.

Run this against a freshly seeded database:

    python -m loadtest.run_seed_check

It guards the F1 regression: no booking may point at a room type different from
its physical room, and no two room types may share a display name.
"""

import sys

from .lib import seed


CHECKS = [
    (
        "房间总数 = 293",
        "SELECT COUNT(*) AS n FROM rooms",
        293,
    ),
    (
        "房型总数 = 6（无同名重复）",
        "SELECT COUNT(*) AS n FROM room_types",
        6,
    ),
    (
        "同名房型分组 = 0",
        "SELECT COUNT(*) AS n FROM ("
        " SELECT name FROM room_types GROUP BY name HAVING COUNT(*) > 1"
        ") duplicates",
        0,
    ),
    (
        "演示订单数 = 7",
        "SELECT COUNT(*) AS n FROM bookings b JOIN users u ON u.id = b.actor_user_id "
        "WHERE u.email = 'desk@example.test'",
        7,
    ),
    (
        "订单房型与实体房房型不一致的订单 = 0",
        "SELECT COUNT(*) AS n FROM bookings b JOIN rooms r ON r.id = b.room_id "
        "WHERE b.room_type_id <> r.room_type_id",
        0,
    ),
    (
        "订单房型与实体房房型不一致的房晚 = 0",
        "SELECT COUNT(*) AS n FROM room_nights rn "
        "JOIN bookings b ON b.id = rn.booking_id "
        "JOIN rooms r ON r.id = rn.room_id "
        "WHERE b.room_type_id <> r.room_type_id",
        0,
    ),
    (
        "入住人数超过房型容量的订单 = 0",
        "SELECT COUNT(*) AS n FROM bookings b JOIN room_types rt ON rt.id = b.room_type_id "
        "WHERE b.guests > rt.capacity",
        0,
    ),
    (
        "演示订单总额与房型基础价 × 晚数不一致 = 0",
        "SELECT COUNT(*) AS n FROM bookings b "
        "JOIN room_types rt ON rt.id = b.room_type_id "
        "JOIN users u ON u.id = b.actor_user_id "
        "WHERE u.email = 'desk@example.test' "
        "AND b.total <> rt.base_price * DATEDIFF(b.check_out, b.check_in)",
        0,
    ),
    (
        "逐晚金额合计与订单总额不一致 = 0",
        "SELECT COUNT(*) AS n FROM ("
        " SELECT b.id FROM bookings b JOIN booking_price_lines pl ON pl.booking_id = b.id"
        " GROUP BY b.id, b.total HAVING SUM(pl.amount) <> b.total"
        ") mismatched",
        0,
    ),
]


def main():
    failures = []
    with seed.connect() as connection:
        with connection.cursor() as cursor:
            print(f"{'检查项':<48} {'期望':>6} {'实测':>6}  结果")
            for title, sql, expected in CHECKS:
                cursor.execute(sql)
                actual = int(cursor.fetchone()["n"])
                passed = actual == expected
                if not passed:
                    failures.append((title, expected, actual))
                print(f"{title:<48} {expected:>6} {actual:>6}  "
                      f"{'PASS' if passed else 'FAIL'}")
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} seed checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
