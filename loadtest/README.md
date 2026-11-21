# HTTP concurrency and stress-test harness (`loadtest`)

This directory is a black-box HTTP test harness kept separate from the application.
It covers **L6 (HTTP concurrency/conflict)** and **L7 (stress)** in the Milestone 2
report. Every virtual user follows the real browser flow — login form, CSRF, search,
confirmation page, booking POST — and never calls a Python service function directly.

## Layout

```text
loadtest/
├── config.py                 base URL, database, accounts and fixture settings
├── serve_app.py              local test server (larger DB pool only)
├── run_conflict.py           same-room contention + idempotency + false sold-out
├── run_stress.py             S-browse / S-book / S-mixed
├── run_ramp.py               stepwise virtual-user ramp
├── run_seed_check.py         F1 seed-data regression checks (9)
├── run_dbcheck.py            I1–I15 consistency SQL
├── lib/
│   ├── http.py               per-VU session; login, CSRF, search, booking
│   ├── metrics.py            thread-safe samples, percentiles, CSV/JSON
│   ├── seed.py               test accounts, contention fixture, cleanup
│   └── dbcheck.py            15 invariants plus MySQL status counters
├── scenarios/
│   ├── conflict.py           barrier-synchronised 2/20-client contention
│   └── stress.py             browse, booking and mixed virtual-user loops
├── tools/
│   ├── make_charts.py        raw JSON → report charts (`--lang zh|en`)
│   └── html_to_docx.py       report HTML → DOCX/PDF layout
└── results/                  raw CSV/JSON, pytest baseline and SQL evidence
```

## Prerequisites

1. MySQL 8 is running and a **dedicated test database** exists. Do not point the
   harness at the development database `hero`.
2. Dependencies are installed and the database is migrated and seeded:

   ```powershell
   $env:SECRET_KEY = "loadtest-secret"
   $env:DATABASE_URL = "mysql+pymysql://hero_app:HeroApp2026@127.0.0.1:3306/hero_test_20261116?charset=utf8mb4"
   python -m flask --app app db upgrade
   python -m flask --app app seed-demo
   python -m flask --app app seed-rooms
   python -m flask --app app seed-desk
   ```

3. Start the application under test on port 5001. Werkzeug supplies threads via
   `threaded=True`; the test launcher enlarges the SQLAlchemy pool to 20 + 40 overflow:

   ```powershell
   $env:SECRET_KEY = "loadtest-secret"
   $env:DATABASE_URL = "mysql+pymysql://hero_app:HeroApp2026@127.0.0.1:3306/hero_test_20261116?charset=utf8mb4"
   python -m loadtest.serve_app
   ```

4. Test accounts are created automatically:
   - booking/contention: `vu001@load.test` … (password `Load2026!`)
   - idempotency: `idem001@load.test`
   - manager for screenshots: `manager@load.test` (password `Manager2026!`)
   - front-desk demo account from `seed-desk`: `desk@example.test / Desk2026!`

## Running

```powershell
# Same-room contention: 4 two-client rounds + 3 twenty-client rounds + 10-way idempotency
python -m loadtest.run_conflict --two-clients 4 --heavy-clients 20 --heavy-rounds 3

# Single-scenario stress
python -m loadtest.run_stress --scenario browse --users 20 --duration 45
python -m loadtest.run_stress --scenario book   --users 15 --duration 60
python -m loadtest.run_stress --scenario mixed  --users 20 --duration 60

# Ramps
python -m loadtest.run_ramp --scenario browse --users 1,5,10,20,40 --duration 30
python -m loadtest.run_ramp --scenario book   --users 1,5,10,20 --duration 20

# Consistency and seed checks
python -m loadtest.run_dbcheck
python -m loadtest.run_seed_check

# Report charts (English or Chinese labels)
python loadtest/tools/make_charts.py --lang en --results loadtest/results --output <dir>
```

`run_stress` and `run_ramp` delete bookings created by previous `*@load.test` runs so
the same database can be reused. `run_conflict` creates a one-room fixture
(`lt-conflict` / `LT901`) to concentrate contention on one `rooms` row; remove it
afterwards with:

```powershell
python -c "from loadtest.lib import seed; seed.remove_conflict_fixture()"
```

This keeps the demo UI at 293 rooms.

## Design notes

- **One session per VU:** avoids turning concurrency into serialised shared-session
  traffic. Login parses the real CSRF token; CSRF is never disabled.
- **Fresh idempotency key per request:** each confirmation page generates a new key,
  so a load run cannot accidentally create only one booking. The idempotency test
  deliberately reuses one key.
- **Barrier synchronisation:** every confirmation page is prepared before the barrier;
  POSTs are released together, so the test measures database locking rather than HTTP
  client ordering.
- **Outcome classification:** `success`, `sold_out`, `busy`, `price_changed`,
  `idempotency_mismatch` and `http_error`, so a business refusal is not counted as a
  server error.
- **False sold-out probe:** the contested date must return zero availability while a
  different free date must still return one room. This proves the sold-out result is
  real inventory, not a global misclassification.
- **Consistency SQL on a separate connection:** no application Session is reused.
  The 15 checks cover overbooking, overlapping stays, room-night counts, amounts,
  maintenance conflicts, duplicate idempotency records, room-type mismatch and duplicate
  room-type names.
- **F1 regression:** `run_seed_check.py` checks 9 properties of the demo seed data.
  `seed-desk` now reuses the physical room type, repairs legacy demo bookings on repeat
  execution and removes unreferenced duplicate room types.

## Known limitations

- The application under test is `flask run` with `threaded=True`, not a production WSGI
  server. The throughput ceiling mainly reflects a single-process Python server, not
  InnoDB.
- The load generator and MySQL share the same machine, so client threads and the
  database compete for CPU. Absolute RPS values are for comparison within this build,
  not production-capacity claims.
