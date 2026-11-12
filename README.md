# The Harbour Hotel

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
python -m flask --app app create-manager manager@example.test
python -m flask --app app run --host 127.0.0.1 --port 5000
```

The manager command prompts for a password. Run it once for the initial manager.
Sign in at http://127.0.0.1:5000/login. Guests can create accounts at /register.
Managers can create front-desk accounts from Staff accounts.

## Workspaces

- /guest/ — search and quote an entire stay, submit a reservation and view your own
  bookings. Quotes are recalculated on submission. A changed price requires a fresh
  confirmation. A confirmed reservation stores an amount for each night.
- /manage — operations overview, room types and room inventory, seasonal and
  date-specific prices, staff accounts, customer blacklist, bookings, exception
  cancellation, audit, occupancy reports and CSV export. Manager writes are transactional.
  Staff accounts are disabled without removing their historical references.
- /frontdesk — sign-in destination for front-desk accounts. Check-in, check-out,
  walk-ins, request approval and collection of simulated payments remain front-desk
  integration work. The manager console uses the shared request, payment and actual-stay
  records and shows zero or an empty state when they have no data.

The occupancy report uses completed Beijing-time nights from each room's operating
period and actual stay records, with effective maintenance deducted once per room
and night. Upcoming reservations appear separately. The CSV calls the same report
function as the page. Cancellation refunds the net successful simulated payments and
releases allocated nights in the same transaction, retaining the original booking and
price lines. No real payment provider is used.

## MySQL integration tests

Install requirements-dev.txt and create a separate empty MySQL database. Point
TEST_DATABASE_URL to it before running python -m pytest. The test suite applies the
migrations to the empty database and leaves the test data in place. Use another empty
test database for the next run. Without this variable the MySQL tests are skipped.
