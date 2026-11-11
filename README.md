# Hotel Hero

Hotel Hero is the Group 6 hotel booking project for COMP3019J. Increment 1 delivers the shared Flask application, guest registration, login and password management for three roles, manager staff-account controls, room catalogue, whole-stay availability and nightly quotes. Business writes record audit entries from the first increment.

## Run locally

Use Python 3.12 and MySQL Community Server 8.0.45 with InnoDB. The browser uses Jinja templates, HTML, CSS and plain JavaScript. The search form also works with JavaScript disabled.

Create a development database with the `utf8mb4` character set and use a MySQL account with access to that schema. From the repository root, run the following in PowerShell, replacing the database credentials with your own. URL-encode special characters in the password.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DATABASE_URL = 'mysql+pymysql://hero_app:YOUR_PASSWORD@127.0.0.1:3306/hero?charset=utf8mb4'
$env:SECRET_KEY = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
.\.venv\Scripts\python.exe -m flask --app app db upgrade
.\.venv\Scripts\python.exe -m flask --app app seed-demo
.\.venv\Scripts\python.exe -m flask --app app bootstrap-manager manager@example.test
.\.venv\Scripts\python.exe -m flask --app app run
```

Open `http://127.0.0.1:5000`. Register a guest through the site. The manager command prompts for a password. Sign in as the manager to create a front-desk staff account. Re-running the catalogue seed preserves existing demo records.

Set `SESSION_COOKIE_SECURE=1` when serving the app over HTTPS. Keep the secret key and database credentials in the deployment environment.

## Integration points

- `app/__init__.py` creates the Flask app and initializes extensions. Account and catalogue blueprints share the base template and database configuration.
- `app/accounts.py` loads active accounts for Flask-Login, enforces role permissions and resolves the guest's customer record. The manager account controls operate on staff accounts.
- `app/catalog.py` supplies the read-only catalogue and availability endpoint. It checks Beijing arrival dates and capacity, finds one physical room for the full half-open stay, and quotes date-specific, seasonal and base rates in that order. Quotes use decimal amounts and display consecutive nights at the same rate together.
- `app/audit.py` writes audit entries using the caller's transaction. `app/blacklist.py` checks effective restrictions on a customer record.
- `app/allocation.py` provides the internal guest allocation used for the three checks scheduled before increment 2. It uses a dedicated READ COMMITTED session, candidate-room locking, per-night uniqueness and actor-scoped idempotency. The public booking workflow is scheduled for increment 2. Manager audit queries, blacklist administration and reporting are scheduled for later increments.

## Verify with MySQL

The integration suite uses a dedicated MySQL 8 instance. It creates an isolated schema per test, applies the committed migration, and drops that schema at teardown. Use a test-server account allowed to create and drop those schemas. The suite exercises account permissions, CSRF, seed data, whole-stay search, price priority and date boundaries. Two independent database sessions verify last-room competition, a failure after a night has been written and duplicate request replay.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:TEST_DATABASE_URL = 'mysql+pymysql://test_user:YOUR_PASSWORD@127.0.0.1:3306/?charset=utf8mb4'
.\.venv\Scripts\python.exe -m pytest -q
```
