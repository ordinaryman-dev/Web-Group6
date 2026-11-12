from functools import wraps
from posixpath import normpath
from urllib.parse import unquote, urlsplit

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import EqualTo, InputRequired, Length, Optional, Regexp

from .audit import record_audit
from .extensions import db, login_manager
from .models import Customer, GUEST, MANAGER, STAFF, User


auth = Blueprint("auth", __name__)


def trim(value):
    return value.strip() if value else value


def normalize_email(value):
    return trim(value).casefold() if value else value


class LoginForm(FlaskForm):
    email = StringField(
        "Email",
        filters=[normalize_email],
        validators=[InputRequired(), Length(max=254)],
    )
    password = PasswordField("Password", validators=[InputRequired(), Length(max=128)])
    submit = SubmitField("Sign in")


class RegistrationForm(FlaskForm):
    name = StringField("Full name", filters=[trim], validators=[InputRequired(), Length(min=1, max=120)])
    email = StringField(
        "Email",
        filters=[normalize_email],
        validators=[
            InputRequired(),
            Length(max=254),
            Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Enter a valid email address."),
        ],
    )
    phone = StringField("Phone (optional)", filters=[trim], validators=[Optional(), Length(max=40)])
    password = PasswordField("Password", validators=[InputRequired(), Length(min=8, max=128)])
    confirm = PasswordField(
        "Confirm password",
        validators=[InputRequired(), EqualTo("password", message="Passwords must match.")],
    )
    submit = SubmitField("Create account")


@login_manager.user_loader
def load_user(user_id):
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    return user if user and user.active else None


def role_required(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def authorized(*args, **kwargs):
            if current_user.role not in roles:
                abort(403, description="Your account cannot open this page.")
            return view(*args, **kwargs)

        return authorized

    return decorate


def destination(user):
    return {GUEST: "guest.index", STAFF: "auth.not_ready", MANAGER: "manager.overview"}[user.role]


def safe_next(target, user):
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    try:
        path = normpath(unquote(urlsplit(target).path))
    except ValueError:
        return None
    if "\\" in path or any(ord(character) < 32 for character in path):
        return None
    root = {GUEST: "/guest", STAFF: "/frontdesk", MANAGER: "/manage"}[user.role]
    return target if path == root or path.startswith(root + "/") else None


@auth.get("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    return redirect(url_for(destination(current_user)))


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(destination(current_user)))

    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalar(db.select(User).where(User.email == form.email.data))
        if user and user.active and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            return redirect(safe_next(request.args.get("next"), user) or url_for(destination(user)))
        form.password.errors.append("Email or password is incorrect.")

    status = 401 if form.is_submitted() else 200
    return render_template("auth/login.html", form=form), status


@auth.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for(destination(current_user)))

    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            with Session(db.engine) as session, session.begin():
                user = User(
                    email=form.email.data,
                    password_hash=generate_password_hash(form.password.data),
                    role=GUEST,
                )
                session.add(Customer(user=user, name=form.name.data, phone=form.phone.data or None))
                session.flush()
                record_audit(session, user.id, "register", "user", user.id)
        except IntegrityError:
            form.email.errors.append("This email cannot be registered.")
            return render_template("auth/register.html", form=form), 409

        flash("Account created. Sign in to continue.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form)


@auth.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth.get("/workspace")
@auth.get("/frontdesk")
@login_required
def not_ready():
    if current_user.role != STAFF:
        return redirect(url_for(destination(current_user)))
    titles = {STAFF: "Front desk", MANAGER: "Manager console"}
    return render_template("auth/not_ready.html", role_title=titles.get(current_user.role, "Workspace"))
