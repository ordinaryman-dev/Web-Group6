# Hero Hotel

COMP3019J Group 6. The Flask application shares one MySQL 8 / InnoDB database across
the guest portal and manager console. Pages use server-rendered Jinja, HTML and CSS.
Forms use Flask-WTF CSRF protection and Flask-Login sessions.

## Local setup

Use Python 3.12 and a MySQL 8 database with the utf8mb4 character set. From the
repository root, with a local virtual environment activated and its dependencies installed:

```powershell
python -m pip install -r requirements.txt
$env:SECRET_KEY = '<your randomly generated development secret>'
$env:DATABASE_URL = 'mysql+pymysql://<user>:<password>@127.0.0.1:3306/<database>?charset=utf8mb4'
python -m flask --app app db upgrade
python -m flask --app app seed-demo
python -m flask --app app seed-rooms
python -m flask --app app seed-desk
python -m flask --app app create-manager manager@example.test
python -m flask --app app run --host 127.0.0.1 --port 5000
```

The manager command prompts for a password. Run it once for the initial manager.
Sign in at http://127.0.0.1:5000/login. Guests can create accounts at /register.
Managers can create front-desk accounts from Staff accounts.

The inventory is 293 rooms: 12 floors × 24 rooms plus the five-room demo wing. An
older database that still carries a Family Suite numbered x25 on every floor can be
converged with `python -m flask --app app retire-legacy-rooms --apply`. The command
moves active stays to a free room of the same type and keeps the old booking history.

## Schema

Migrations are applied with `python -m flask --app app db upgrade`. The current head is
`20261121_11`. Besides the original catalogue and booking tables, the migrations add
booking notes/timestamps, manager request/payment records and actual-stay fields, the
front-desk expected-arrival field, `room_types.bed_type` (`single`, `double`, `twin`,
`king`, `studio`, `suite`) used by the guest filter, `rooms.housekeeping` (`clean` / `dirty`) and
`rooms.has_window` used by the front-desk ventilation filter, microsecond-precision
`rooms.last_allocated_at` used for allocation rotation, the snapped
`bookings.cancel_by` cancellation deadline, and `payment_records.idempotency_key`
with its per-actor unique constraint, and `customer_phone_locks` to coordinate
walk-in registration. After pulling this change, run
`python -m flask --app app db upgrade` before starting the site. The rotation migration
initialises existing rooms from the latest known booking creation time; rooms without
orders remain unassigned. Subsequent successful assignments record their own timestamps.

## Workspaces

- /guest/ — hero search with bed-type filters, three-column room grid, a separate
  confirmation page, status tabs on My bookings, a 70/30 order detail page with note editing
  and room-type/date/extension/cancellation request modals, and an account dashboard for
  contact details and password changes. Quotes
  are recalculated on submission. A changed price requires a fresh confirmation. A confirmed
  reservation stores an amount for each night.
- /manage — operations overview, room types and room inventory, seasonal and
  date-specific prices, staff accounts, customer blacklist, bookings, exception
  cancellation, audit, occupancy reports and CSV export. Manager writes are transactional.
  Room records include the window flag used by the desk filter; staff accounts can be
  disabled, re-enabled and given a new password without removing their history.
- /frontdesk — front-desk workbench. A narrow section menu opens three views: the room
  board (12 floors of 24 rooms plus the five-room demo wing, 293 rooms), the order list and the task queue. The
  order list is paged; maintenance dates preview their booking conflicts before saving. The
  top bar keeps global search (name, phone, room or `BK-` reference), a 15-day timeline and
  a live Beijing clock. Clicking a room or order opens a 400px action drawer for check-in,
  checkout, walk-in, simulated payment/refund, maintenance blocks and request decisions.
  Housekeeping marks a room dirty at checkout and blocks selling it until it is marked
  clean. The grid filters by state, bed type, room type, window and whole-stay availability.
  Check-in still requires a confirmed same-day booking, 14:00 or later, no maintenance
  block, a clean room and full simulated payment. No real payment provider is used.

The front-desk account menu includes `/frontdesk/profile` for a staff member's own
password change, with current-password validation and auditing. Walk-in registration
ignores spaces, brackets and hyphens when looking for an existing phone. A matching
number requires staff to select an existing guest; shared numbers are not silently
merged or made globally unique. Restricted guests are marked in the selection list
and checked again by the booking service. Concurrent new registrations for the same
formatted number are coordinated by a transaction-scoped row lock.

Maintenance can be registered from both room and booking drawers, including reserved
and occupied rooms; existing reservations remain intact and conflicts are previewed.
Checkout, no-show and cancellation close pending requests in the same transaction,
recording the reason and handler. Request decisions and withdrawal take the booking
lock before the request lock. The upgrade also closes legacy pending requests whose
orders had already ended, identifying the automatic repair in their comments.

The occupancy report uses completed Beijing-time nights from each room's operating
period and actual stay records, with effective maintenance deducted once per room
and night. Upcoming reservations appear separately. The CSV calls the same report
function as the page. Cancellation refunds the net successful simulated payments and
releases allocated nights in the same transaction, retaining the original booking and
price lines. No real payment provider is used.

## Automatic room allocation

Guest reservations, automatic front-desk walk-ins, maintenance reassignments and approved
room-type/date changes use `app/allocation.py`. First filter to rooms of the required
type that cover the entire stay, respect operating dates and maintenance, have no
overlapping booking or overdue occupant, and are clean for a same-day arrival.

For each eligible room, compute its **room-night load** in Beijing time:

```text
load = actual occupied nights in [today - 30 days, today)
     + committed reservation nights in [today, infinity)
```

Each retained `RoomNight` contributes 1, regardless of guest count or price. Historical
nights require a checked-in/checked-out booking and fall between its actual Beijing
arrival/departure dates. The second term includes every retained future/current night
of confirmed or checked-in bookings, including long stays whose departure extends
beyond the 14-day arrival-booking window. The date ranges do not overlap. Cancelled,
no-show and unfulfilled past reservations contribute no load; released nights no
longer count. Overdue nights without a `RoomNight` are not
invented for this score; a still-occupied room remains excluded by availability checks.

Choose the lowest load, then the oldest successful `last_allocated_at` (never assigned
first), then room ID as a stable final tie-breaker. For example, loads 8 / 3 / 3 choose
one of the last two rooms, preferring the one allocated longer ago. This balances
booked nights rather than reservation counts and does not force floor-by-floor placement.
During a replacement, exclude the replaced booking from both availability and load.
An explicitly selected walk-in room is honoured after the same availability/locking
checks; it also updates rotation history. An extension retains its existing room,
increases load by the added nights and does not reset the assignment timestamp.

Allocation uses InnoDB `FOR UPDATE` locks and READ COMMITTED transactions. Select one
candidate, lock it, then query current availability/ranking again. Reassignments lock
the original and target rooms in ascending ID order. A stale candidate aborts the
whole attempt instead of keeping its lock while trying other rooms. Room-type shared
locks coordinate with catalogue/price edits; maintenance creation locks its room.
The unique `(room_id, stay_date)` constraint remains the final overbooking guard.

Booking, room nights, price lines, payment/audit writes and the rotation timestamp
commit together. Failure rolls everything back, including a newly registered walk-in
customer. Idempotent booking replay neither adds nights nor advances rotation.
MySQL deadlock (1213), lock timeout (1205), stale candidates and room-night uniqueness
collisions retry with a fresh Session, at most three attempts with short backoff.
Business refusals are not retried. Exhausted contention returns a busy/retry message;
it is not reported as sold out and does not leave partial writes.

## MySQL integration tests

Install requirements-dev.txt and create a separate empty MySQL database. Point
TEST_DATABASE_URL to it before running python -m pytest. The test suite applies the
migrations to the empty database and leaves the test data in place. Use another empty
test database for the next run. Without this variable the MySQL tests are skipped.
