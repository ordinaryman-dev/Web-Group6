# 压力场景：S-browse（只读浏览）、S-book（登录后真实下单）、S-mixed（浏览+下单+查单混合）。
"""Virtual-user loops for the three planned stress scenarios."""

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from ..config import BASE_URL, in_days, iso
from ..lib import seed
from ..lib.http import GuestClient, public_api_client
from ..lib.metrics import Recorder


def _stay(rng, first_offset=2, last_offset=12):
    check_in = in_days(rng.randint(first_offset, last_offset))
    nights = rng.choice([1, 1, 2, 2, 3])
    return check_in, check_in + timedelta(days=nights)


def _record(recorder, action, vu, response, outcome=None, started=None):
    latency = (time.perf_counter() - started) * 1000 if started else 0.0
    code = getattr(response, "status_code", 0)
    if outcome is None:
        outcome = "ok" if code < 400 else ("http_error" if code >= 500 else "client_error")
    recorder.record(action=action, vu=vu, status_code=code, latency_ms=latency,
                    outcome=outcome)


def _run_workers(workers, users, duration, recorder):
    deadline = time.time() + duration
    barrier = threading.Barrier(users)

    def worker(index):
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            return
        workers(index + 1, deadline)

    with ThreadPoolExecutor(max_workers=users) as pool:
        list(pool.map(worker, range(users)))


def run_browse(users=20, duration=45, base_url=BASE_URL, output_dir="loadtest/results",
               label=None):
    """S-browse: anonymous HTML search and JSON search, no database writes."""

    recorder = Recorder("browse", users=users, label=label or f"browse_{users}u")
    rng = random.Random(20261121 + users)
    public = public_api_client(base_url)

    def worker(vu, deadline):
        local_rng = random.Random(rng.random())
        while time.time() < deadline:
            check_in, check_out = _stay(local_rng)
            started = time.perf_counter()
            if local_rng.random() < 0.7:
                response = public.search_api(iso(check_in), iso(check_out), guests=1)
                _record(recorder, "api_search", vu, response, started=started)
            else:
                response = public.search_page(
                    iso(check_in), iso(check_out), guests=1,
                    bed_type=local_rng.choice(["all", "double", "king", "twin"]),
                )
                _record(recorder, "search_page", vu, response, started=started)
            time.sleep(0.05)

    recorder.start()
    _run_workers(worker, users, duration, recorder)
    recorder.stop()
    csv_path, json_path = recorder.write(output_dir)
    return recorder.summary(), csv_path, json_path


def _login_clients(users, base_url):
    return seed.login_clients(users, base_url, prefix="vu")


def _do_booking(recorder, client, vu, rng):
    """One full booking attempt: pick a type, open confirmation, post the form."""

    check_in, check_out = _stay(rng, first_offset=2, last_offset=12)
    started = time.perf_counter()
    response = client.search_api(iso(check_in), iso(check_out), guests=1)
    _record(recorder, "api_search", vu, response, started=started)
    if response.status_code != 200:
        return False
    try:
        offers = [item for item in response.json().get("room_types", [])
                  if item.get("available_count", 0) > 0]
    except ValueError:
        recorder.record("api_search", vu, response.status_code, 0, "invalid_json")
        return False
    if not offers:
        return False
    offer = rng.choice(offers)

    started = time.perf_counter()
    fields, latency, messages = client.prepare_booking(
        offer["id"], iso(check_in), iso(check_out), guests=1
    )
    recorder.record(
        "prepare", vu, 200 if fields else 0, latency,
        "ok" if fields else "unavailable",
        detail="; ".join(message for _, message in messages)[:240],
    )
    if fields is None:
        return False

    result = client.submit_booking(fields)
    recorder.record(
        "book", vu, result["status_code"], result["latency_ms"], result["outcome"],
        detail=result["detail"], extra={"booking_id": result["booking_id"]},
    )
    return result["outcome"] == "success"


def run_book(users=15, duration=60, base_url=BASE_URL, output_dir="loadtest/results",
             label=None):
    """S-book: authenticated guests search and create real bookings."""

    recorder = Recorder("book", users=users, label=label or f"book_{users}u")
    rng = random.Random(20261122 + users)
    clients = _login_clients(users, base_url)

    def worker(vu, deadline):
        local_rng = random.Random(rng.random())
        client = clients[vu - 1]
        while time.time() < deadline:
            try:
                _do_booking(recorder, client, vu, local_rng)
            except Exception as exc:  # noqa: BLE001
                recorder.record("book", vu, 0, 0, "http_error",
                                detail=f"{type(exc).__name__}: {exc}")
            time.sleep(local_rng.uniform(0.8, 1.6))

    recorder.start()
    _run_workers(worker, users, duration, recorder)
    recorder.stop()
    csv_path, json_path = recorder.write(output_dir)
    return recorder.summary(), csv_path, json_path


def run_mixed(users=20, duration=60, base_url=BASE_URL, output_dir="loadtest/results",
              label=None):
    """S-mixed: 55% API search, 15% page search, 25% booking, 5% own bookings."""

    recorder = Recorder("mixed", users=users, label=label or f"mixed_{users}u")
    rng = random.Random(20261123 + users)
    clients = _login_clients(users, base_url)

    def worker(vu, deadline):
        local_rng = random.Random(rng.random())
        client = clients[vu - 1]
        public = public_api_client(base_url)
        while time.time() < deadline:
            roll = local_rng.random()
            try:
                if roll < 0.55:
                    check_in, check_out = _stay(local_rng)
                    started = time.perf_counter()
                    response = public.search_api(iso(check_in), iso(check_out), guests=1)
                    _record(recorder, "api_search", vu, response, started=started)
                elif roll < 0.70:
                    check_in, check_out = _stay(local_rng)
                    started = time.perf_counter()
                    response = public.search_page(iso(check_in), iso(check_out), guests=1)
                    _record(recorder, "search_page", vu, response, started=started)
                elif roll < 0.95:
                    _do_booking(recorder, client, vu, local_rng)
                else:
                    started = time.perf_counter()
                    response = client.bookings_page()
                    _record(recorder, "bookings_page", vu, response, started=started)
            except Exception as exc:  # noqa: BLE001
                recorder.record("mixed", vu, 0, 0, "http_error",
                                detail=f"{type(exc).__name__}: {exc}")
            time.sleep(local_rng.uniform(0.2, 0.8))

    recorder.start()
    _run_workers(worker, users, duration, recorder)
    recorder.stop()
    csv_path, json_path = recorder.write(output_dir)
    return recorder.summary(), csv_path, json_path
