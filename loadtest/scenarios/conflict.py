# 并发冲突测试：多客户端同时抢同一间房，验证“只成功一单”、无超售、无假售罄与幂等重放。
"""Same-room contention and idempotency tests over real HTTP."""

import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from ..config import BASE_URL, day_range, iso
from ..lib import seed
from ..lib.http import GuestClient, hidden_fields, public_api_client
from ..lib.metrics import Recorder


def _room_booking_counts(room_id, check_in, check_out):
    """Snapshot the active bookings/nights for one room and stay window."""

    query = """
        SELECT COUNT(DISTINCT b.id) AS bookings,
               COUNT(rn.id) AS nights
        FROM bookings b
        LEFT JOIN room_nights rn
               ON rn.booking_id = b.id
              AND rn.stay_date >= %s AND rn.stay_date < %s
        WHERE b.room_id = %s AND b.status IN ('confirmed', 'checked_in')
          AND b.check_in < %s AND b.check_out > %s
    """
    with seed.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (check_in, check_out, room_id, check_out, check_in))
            return cursor.fetchone()


def _fire_together(clients, fields_list, timeout=40):
    """Release every client at the same instant and collect their POST results."""

    barrier = threading.Barrier(len(clients))

    def attempt(index):
        client = clients[index]
        fields = fields_list[index]
        try:
            barrier.wait(timeout=timeout)
        except threading.BrokenBarrierError:
            return {
                "status_code": 0, "latency_ms": 0.0, "outcome": "barrier_broken",
                "booking_id": None, "detail": "barrier timeout", "url": "",
            }
        try:
            return client.submit_booking(fields)
        except Exception as exc:  # noqa: BLE001 - every failure shape belongs in the report
            return {
                "status_code": 0, "latency_ms": 0.0, "outcome": "http_error",
                "booking_id": None, "detail": f"{type(exc).__name__}: {exc}", "url": "",
            }

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        return list(pool.map(attempt, range(len(clients))))


def _run_round(recorder, clients, room_id, room_type_id, check_in, check_out,
               action, round_no, nights):
    prepared = []
    prepare_latencies = []
    failures = []
    for index, client in enumerate(clients):
        fields, latency, messages = client.prepare_booking(
            room_type_id, iso(check_in), iso(check_out), guests=1
        )
        prepare_latencies.append(latency)
        if fields is None:
            failures.append({"vu": index + 1, "messages": messages})
        else:
            prepared.append((index, client, fields))

    if failures:
        for failure in failures:
            recorder.record(
                action=f"{action}_prepare", vu=failure["vu"], status_code=0,
                latency_ms=0, outcome="prepare_failed",
                detail=json.dumps(failure["messages"], ensure_ascii=False),
            )
        return {
            "round": round_no, "kind": action, "aborted": True,
            "check_in": iso(check_in), "check_out": iso(check_out),
            "prepared": len(prepared), "prepare_failures": failures,
        }

    clients_ready = [item[1] for item in prepared]
    fields_ready = [item[2] for item in prepared]
    results = _fire_together(clients_ready, fields_ready)
    for result in results:
        recorder.record(
            action=action, vu=0, status_code=result["status_code"],
            latency_ms=result["latency_ms"], outcome=result["outcome"],
            detail=result["detail"], extra={"booking_id": result["booking_id"]},
        )

    counts = Counter(result["outcome"] for result in results)
    booking_ids = sorted({
        result["booking_id"] for result in results if result["booking_id"]
    })
    db_counts = _room_booking_counts(room_id, check_in, check_out)
    prepare_sorted = sorted(prepare_latencies)
    return {
        "round": round_no,
        "kind": action,
        "check_in": iso(check_in),
        "check_out": iso(check_out),
        "prepared": len(prepared),
        "clients": len(clients),
        "outcomes": dict(counts),
        "successes": counts.get("success", 0),
        "only_one_booking": counts.get("success", 0) == 1,
        "distinct_booking_ids": booking_ids,
        "prepare_latency_p50_ms": round(prepare_sorted[len(prepare_sorted) // 2], 2),
        "post_latency_ms": sorted(round(result["latency_ms"], 2) for result in results),
        "db": {
            "bookings_for_round": int(db_counts["bookings"]),
            "nights_for_round": int(db_counts["nights"]),
            "expected_nights": nights,
        },
    }


def _idempotency_test(recorder):
    """Same guest, same key, ten parallel submissions, then a changed body."""

    fixture = seed.ensure_conflict_fixture()
    room_type_id = fixture["room_type_id"]
    check_in, check_out = day_range(5, 1)
    accounts = seed.ensure_guest_accounts(1, prefix="idem")
    clients = [
        GuestClient(accounts[0]["email"], accounts[0]["password"], BASE_URL)
        for _ in range(10)
    ]
    for client in clients:
        client.login()

    first, _, messages = clients[0].prepare_booking(
        room_type_id, iso(check_in), iso(check_out), guests=1
    )
    if first is None:
        return {"aborted": True, "prepare_failures": messages}

    shared_fields = []
    for client in clients:
        own, _, _ = client.prepare_booking(
            room_type_id, iso(check_in), iso(check_out), guests=1
        )
        if own is None:
            return {"aborted": True, "prepare_failures": ["confirmation page vanished"]}
        payload = dict(first)
        payload["csrf_token"] = own["csrf_token"]
        shared_fields.append(payload)

    results = _fire_together(clients, shared_fields)
    for result in results:
        recorder.record(
            action="book_same_key", vu=0, status_code=result["status_code"],
            latency_ms=result["latency_ms"], outcome=result["outcome"],
            detail=result["detail"], extra={"booking_id": result["booking_id"]},
        )
    booking_ids = sorted({item["booking_id"] for item in results if item["booking_id"]})
    with seed.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM bookings "
                "WHERE actor_user_id = %s AND idempotency_key = %s",
                (accounts[0]["user_id"], first["idempotency_key"]),
            )
            stored = int(cursor.fetchone()["n"])

    # 同一个键配不同请求内容：必须被识别为幂等冲突，不能创建第二张订单。
    mismatch_client = clients[0]
    token = hidden_fields(
        mismatch_client.session.get(
            f"{mismatch_client.base_url}/guest/", timeout=mismatch_client.timeout
        ).text
    ).get("csrf_token", "")
    mismatch_fields = dict(first)
    mismatch_fields["csrf_token"] = token
    mismatch_fields["note"] = "a different request"
    try:
        mismatch = mismatch_client.submit_booking(mismatch_fields)
    except Exception as exc:  # noqa: BLE001
        mismatch = {
            "status_code": 0, "latency_ms": 0, "outcome": "http_error",
            "booking_id": None, "detail": f"{type(exc).__name__}: {exc}",
        }
    recorder.record(
        action="book_same_key_different_body", vu=0,
        status_code=mismatch["status_code"], latency_ms=mismatch["latency_ms"],
        outcome=mismatch["outcome"], detail=mismatch["detail"],
    )
    with seed.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM bookings "
                "WHERE actor_user_id = %s AND idempotency_key = %s",
                (accounts[0]["user_id"], first["idempotency_key"]),
            )
            stored_after_mismatch = int(cursor.fetchone()["n"])

    return {
        "check_in": iso(check_in),
        "check_out": iso(check_out),
        "parallel_submissions": len(results),
        "outcomes": dict(Counter(item["outcome"] for item in results)),
        "distinct_booking_ids": booking_ids,
        "stored_bookings_for_key": stored,
        "mismatch_outcome": mismatch["outcome"],
        "mismatch_detail": mismatch["detail"],
        "stored_bookings_after_mismatch": stored_after_mismatch,
    }


def _fake_sold_out_probe(room_type_id, booked_check_in, booked_check_out,
                         probe_check_in, probe_check_out):
    """A sold-out stay must not make a different stay look sold out."""

    client = public_api_client(BASE_URL)
    booked = client.search_api(iso(booked_check_in), iso(booked_check_out), guests=1)
    probe = client.search_api(iso(probe_check_in), iso(probe_check_out), guests=1)
    booked_types = (
        {item["id"]: item.get("available_count", 0)
         for item in booked.json().get("room_types", [])}
        if booked.status_code == 200 else {}
    )
    probe_types = (
        {item["id"]: item.get("available_count", 0)
         for item in probe.json().get("room_types", [])}
        if probe.status_code == 200 else {}
    )
    return {
        "booked_stay_available": booked_types.get(room_type_id, 0),
        "probe_stay_available": probe_types.get(room_type_id, 0),
        "booked_status": booked.status_code,
        "probe_status": probe.status_code,
    }


def run(base_url=BASE_URL, two_client_rounds=4, heavy_clients=20, heavy_rounds=3,
        output_dir="loadtest/results"):
    seed.reset_load_bookings()
    fixture = seed.ensure_conflict_fixture()
    room_id = fixture["room_id"]
    room_type_id = fixture["room_type_id"]

    recorder = Recorder("conflict", users=heavy_clients)
    recorder.start()
    rounds = []

    # 双客户端：四组不同日期，每次都应恰好一单成功。
    pair_clients = seed.login_clients(2, base_url, prefix="pair")
    for index in range(two_client_rounds):
        check_in, check_out = day_range(6 + index, 1)
        rounds.append(_run_round(
            recorder, pair_clients, room_id, room_type_id,
            check_in, check_out, "book_2_clients", index + 1, nights=1,
        ))

    # 20 客户端：三轮两晚住宿，验证高并发下仍只有一单进入数据库。
    heavy_clients_list = seed.login_clients(heavy_clients, base_url, prefix="vu")
    for index in range(heavy_rounds):
        check_in, check_out = day_range(10 + index * 2, 2)
        rounds.append(_run_round(
            recorder, heavy_clients_list, room_id, room_type_id,
            check_in, check_out, "book_20_clients", index + 5, nights=2,
        ))

    # 假售罄探针：被抢光的日期应为 0，但另一个空闲日期仍应显示 1 间可售。
    probe_in, probe_out = day_range(4, 1)
    sold_out_checks = []
    for outcome in rounds:
        if outcome.get("aborted"):
            continue
        check = _fake_sold_out_probe(
            room_type_id,
            date.fromisoformat(outcome["check_in"]),
            date.fromisoformat(outcome["check_out"]),
            probe_in, probe_out,
        )
        check["round"] = outcome["round"]
        sold_out_checks.append(check)

    idempotency = _idempotency_test(recorder)
    recorder.stop()

    payload = {
        "fixture": fixture,
        "rounds": rounds,
        "fake_sold_out_checks": sold_out_checks,
        "idempotency": idempotency,
        "summary": recorder.summary(),
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "conflict.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    recorder.write(output_dir)
    return payload
