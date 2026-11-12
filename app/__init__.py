import os
from datetime import timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import Flask, render_template

from .extensions import csrf, db, login_manager, migrate


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL"),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE") == "1",
        MAX_CONTENT_LENGTH=64 * 1024,
        HOTEL_CLOCK=None,
    )
    if config:
        app.config.update(config)
    for setting in ("SECRET_KEY", "SQLALCHEMY_DATABASE_URI"):
        if not app.config[setting]:
            raise RuntimeError(f"Configure {setting} (DATABASE_URL for the database URI).")

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    from .auth import auth
    from .cli import seed_demo
    from .guest import guest

    app.register_blueprint(auth)
    app.register_blueprint(guest)
    app.cli.add_command(seed_demo)

    @app.template_filter("money")
    def money(value):
        return f"{Decimal(value):,.2f}"

    @app.template_filter("localtime")
    def localtime(value, fmt="%d %b %Y, %H:%M"):
        if value is None:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime(fmt)

    @app.errorhandler(400)
    def bad_request(error):
        return render_template("error.html", title="Invalid request", message=error.description), 400

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("error.html", title="Access denied", message=error.description), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template(
            "error.html",
            title="Page not found",
            message="The page you asked for does not exist.",
        ), 404

    return app
