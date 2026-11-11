from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import PasswordField, StringField
from wtforms.validators import InputRequired, Length, Optional, Regexp

from .audit import record_audit
from .extensions import db, login_manager
from .models import Customer, GUEST, MANAGER, STAFF, User


accounts = Blueprint("accounts", __name__)


def trim(value):
    return value.strip() if value else value


def normalize_email(value):
    return trim(value).casefold() if value else value


class LoginForm(FlaskForm):
    email = StringField("Email", filters=[normalize_email], validators=[InputRequired(), Length(max=254)])
    password = PasswordField("Password", validators=[InputRequired(), Length(max=128)])


class AccountForm(FlaskForm):
    email = StringField(
        "Email",
        filters=[normalize_email],
        validators=[
            InputRequired(),
            Length(max=254),
            Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Enter a valid email address."),
        ],
    )
    password = PasswordField("Password", validators=[InputRequired(), Length(min=8, max=128)])


class RegistrationForm(AccountForm):
    name = StringField("Name", filters=[trim], validators=[Length(min=1, max=120)])
    phone = StringField("Phone (optional)", filters=[trim], validators=[Optional(), Length(max=40)])


class PasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[InputRequired(), Length(max=128)])
    new_password = PasswordField("New password", validators=[InputRequired(), Length(min=8, max=128)])


@login_manager.user_loader
def load_user(user_id):
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    return user if user and user.active else None


def require_roles(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def authorized(*args, **kwargs):
            if current_user.role not in roles:
                abort(403, description="Your account cannot perform this action.")
            return view(*args, **kwargs)

        return authorized

    return decorate


def owned_customer(session, user_id):
    return session.scalar(db.select(Customer).where(Customer.user_id == user_id))


def account_form(title, form, submit, status=200):
    return render_template("accounts/form.html", title=title, form=form, submit=submit), status


@accounts.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("accounts.portal"))
    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalar(db.select(User).where(User.email == form.email.data))
        if user and user.active and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            return redirect(url_for("accounts.portal"))
        form.password.errors.append("Email or password is incorrect.")
        return account_form("Sign in", form, "Sign in", 401)
    return account_form("Sign in", form, "Sign in")


@accounts.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("catalog.index"))


@accounts.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("accounts.portal"))
    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            with Session(db.engine) as session, session.begin():
                user = User(email=form.email.data, password_hash=generate_password_hash(form.password.data), role=GUEST)
                session.add(Customer(user=user, name=form.name.data, phone=form.phone.data or None))
                session.flush()
                record_audit(session, user.id, "register", "user", user.id)
        except IntegrityError:
            form.email.errors.append("This email could not be registered.")
            return account_form("Create a guest account", form, "Register", 409)
        flash("Account created. Sign in to continue.", "success")
        return redirect(url_for("accounts.login"))
    return account_form("Create a guest account", form, "Register")


@accounts.get("/portal")
@login_required
def portal():
    titles = {GUEST: "Guest portal", STAFF: "Front desk", MANAGER: "Manager console"}
    return render_template("accounts/portal.html", title=titles[current_user.role])


@accounts.get("/profile")
@login_required
def profile():
    customer = owned_customer(db.session, current_user.id) if current_user.role == GUEST else None
    return render_template("accounts/profile.html", customer=customer)


@accounts.route("/profile/password", methods=["GET", "POST"])
@login_required
def change_password():
    form = PasswordForm()
    if form.validate_on_submit():
        changed = False
        with Session(db.engine) as session, session.begin():
            user = session.get(User, current_user.id, with_for_update=True)
            if user and user.active and check_password_hash(user.password_hash, form.current_password.data):
                user.password_hash = generate_password_hash(form.new_password.data)
                record_audit(session, user.id, "change_password", "user", user.id)
                changed = True
        if changed:
            flash("Password updated.", "success")
            return redirect(url_for("accounts.profile"))
        form.current_password.errors.append("Current password is incorrect.")
        return account_form("Change password", form, "Save password", 400)
    return account_form("Change password", form, "Save password")


@accounts.get("/manager/staff")
@require_roles(MANAGER)
def staff_list():
    staff = db.session.scalars(db.select(User).where(User.role == STAFF).order_by(User.email)).all()
    return render_template("accounts/staff.html", staff=staff)


@accounts.route("/manager/staff/new", methods=["GET", "POST"])
@require_roles(MANAGER)
def create_staff():
    form = AccountForm()
    if form.validate_on_submit():
        try:
            with Session(db.engine) as session, session.begin():
                user = User(email=form.email.data, password_hash=generate_password_hash(form.password.data), role=STAFF)
                session.add(user)
                session.flush()
                record_audit(session, current_user.id, "create_staff", "user", user.id, details={"active": True})
        except IntegrityError:
            form.email.errors.append("This email could not be registered.")
            return account_form("Create staff account", form, "Create account", 409)
        flash("Staff account created.", "success")
        return redirect(url_for("accounts.staff_list"))
    return account_form("Create staff account", form, "Create account")


@accounts.post("/manager/staff/<int:user_id>/disable")
@require_roles(MANAGER)
def disable_staff(user_id):
    with Session(db.engine) as session, session.begin():
        user = session.get(User, user_id, with_for_update=True)
        if user is None or user.role != STAFF:
            abort(404)
        if user.active:
            user.active = False
            record_audit(
                session, current_user.id, "disable_staff", "user", user.id,
                details={"previous_active": True, "active": False},
            )
    flash("Staff account is inactive.", "success")
    return redirect(url_for("accounts.staff_list"))
