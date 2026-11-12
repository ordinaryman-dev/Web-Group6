# The Harbour Hotel — Guest Portal

COMP3019J Group 6. This build keeps the database layer and implements only two pages:

- **Sign in / register** — one entrance for all roles; guests continue to the portal.
- **Guest portal** — search whole-stay availability, quote nightly prices, create a booking,
  and view only the signed-in guest's own bookings.

The front-desk and manager workspaces are intentionally not built yet. Receptionist and
manager accounts can sign in, but land on a "workspace not in this build" page. There is no
cancellation, change-request, payment or reporting flow in this increment.

## Stack

- Flask 3, Jinja templates, plain HTML/CSS (no JavaScript required)
- Flask-Login, Flask-WTF/CSRF, Flask-SQLAlchemy, Flask-Migrate
- MySQL 8 / InnoDB, `utf8mb4`

## Local start

The repository includes the local portable-MySQL scripts used during development:

```powershell
.\start_mysql.ps1      # local MySQL on 127.0.0.1:3306
.\run_dev.ps1          # sets SECRET_KEY and DATABASE_URL, then starts Flask
```

`run_dev.ps1` uses the local development values already configured on this machine. To set
them yourself:

```powershell
$env:SECRET_KEY  = 'dev-secret-key-change-me'
$env:DATABASE_URL = 'mysql+pymysql://hero_app:HeroApp2026@127.0.0.1:3306/hero?charset=utf8mb4'
.\.venv\Scripts\python.exe -m flask --app app db upgrade
.\.venv\Scripts\python.exe -m flask --app app seed-demo
.\.venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1 --port 5000
```

Open `http://127.0.0.1:5000`.

### Demo accounts

| Role | Email | Password | Landing page |
|---|---|---|---|
| Guest | `guest@example.test` | `Guest2026!` | `/guest/` |
| Manager | `manager@example.test` | `Passw0rd!` | `/workspace` placeholder |

`seed-demo` creates the guest account. The manager account above already exists in this local
database from the earlier increment; a fresh database only needs a guest account for this
scope. Public registration creates guest accounts only. Staff accounts are not part of this
build.

## Routes

| Route | Method | Access | Purpose |
|---|---|---|---|
| `/` | GET | public | Redirects to sign-in, guest portal or placeholder |
| `/login` | GET/POST | public | Sign in |
| `/register` | GET/POST | public | Create a guest account |
| `/logout` | POST | signed in | Sign out |
| `/workspace` | GET | staff/manager | Placeholder for the unbuilt workspaces |
| `/guest/` | GET | guest | Search, quote and room cards |
| `/guest/book` | POST | guest | Create a booking |
| `/guest/bookings` | GET | guest | List the guest's own bookings |
| `/guest/bookings/<id>` | GET | guest | Booking confirmation/detail |

## Database

The existing MySQL schema is kept and migrated forward. Migration
`20261112_01_add_booking_note.py` adds two columns to `bookings`:

- `note VARCHAR(100)` — optional guest note, shown in the confirmation.
- `created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP` — booking timestamp, displayed in
  Beijing time.

No extra table was needed for this scope. Login/registration use `users` and `customers`;
search and booking use `room_types`, `rooms`, `price_rules`, `room_blocks`, `bookings`,
`room_nights`, `booking_price_lines` and `audit_logs`. If request/cancellation or payment
flows are added later, they will need their own request/payment records rather than
overloading `bookings`.

## Behaviour implemented

- Search is read-only: it does not hold a room and returns nothing when no single physical
  room covers every night.
- Check-in is limited to today through 14 days ahead; same-day searches close at 22:00
  Beijing time; check-out is exclusive.
- Nightly price priority is date-specific rule, then season rule, then the room type's base
  price. A booking stores one price line per night.
- Booking writes the booking, its room nights and price lines in one transaction, with
  `READ COMMITTED`, pessimistic room locks and an idempotency key. A replay returns the same
  booking; a conflicting key is rejected.
- The guest can see only bookings whose `actor_user_id` is their own.
- Blacklisted customers cannot create a booking.

## Deliberately not built

- Front-desk arrivals/departures, check-in/out, walk-ins, maintenance blocks.
- Manager catalogue/price/account/blacklist/report screens.
- Change, extension, cancellation and refund requests.
- Simulated payments and occupancy reports.
- Automated test suite (the old increment-one tests were removed with the old code).

The previous full increment-one implementation remains recoverable in git commit `e749d53`
and on the local branch `backup/pre-client-reset`.
