from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField, DateField, DecimalField, IntegerField, PasswordField, SelectChoice, SelectField,
    StringField, TextAreaField,
)
from wtforms.validators import DataRequired, EqualTo, InputRequired, Length, NumberRange, Optional, Regexp, ValidationError

from .auth import normalize_email, trim


def currency(form, field):
    value = field.data
    if value is None or not value.is_finite() or not 0 <= value <= Decimal("99999999.99"):
        raise ValidationError("Enter an amount from 0 to 99,999,999.99.")
    if value != value.quantize(Decimal("0.01")):
        raise ValidationError("Use no more than two decimal places.")


class RoomTypeForm(FlaskForm):
    code = StringField("Code", filters=[trim], validators=[DataRequired(), Length(max=32)])
    name = StringField("Room type name", filters=[trim], validators=[DataRequired(), Length(max=100)])
    description = TextAreaField("Description", filters=[trim], validators=[DataRequired(), Length(max=4000)])
    capacity = IntegerField("Capacity", validators=[InputRequired(), NumberRange(min=1, max=2147483647)])
    bed_type = SelectField(
        "Bed type",
        choices=[
            SelectChoice("single", "Single"),
            SelectChoice("double", "Double"),
            SelectChoice("twin", "Twin"),
            SelectChoice("king", "King"),
            SelectChoice("suite", "Suite"),
        ],
        default="double",
        validators=[InputRequired()],
    )
    base_price = DecimalField("Base nightly price (CNY)", validators=[InputRequired(), currency])
    listed = BooleanField("Available for new bookings", default=True)


class RoomForm(FlaskForm):
    number = StringField("Room number", filters=[trim], validators=[DataRequired(), Length(max=20)])
    room_type_id = SelectField("Room type", coerce=int, validators=[InputRequired()])
    operational_from = DateField("First operating date", validators=[InputRequired()])
    operational_until = DateField("First unavailable date (optional)", validators=[Optional()])


class PriceForm(FlaskForm):
    room_type_id = SelectField("Room type", coerce=int, validators=[InputRequired()])
    kind = SelectField("Price rule", choices=[SelectChoice("season", "Seasonal price"), SelectChoice("date", "Date-specific price")])
    start_date = DateField("First night", validators=[InputRequired()])
    end_date = DateField("End date (excluded)", validators=[InputRequired()])
    amount = DecimalField("Nightly price (CNY)", validators=[InputRequired(), currency])


class StaffForm(FlaskForm):
    email = StringField(
        "Staff email", filters=[normalize_email],
        validators=[DataRequired(), Length(max=254), Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")],
    )
    password = PasswordField("Initial password", validators=[InputRequired(), Length(min=8, max=128)])
    confirm = PasswordField("Confirm password", validators=[InputRequired(), EqualTo("password")])


class PasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[InputRequired(), Length(max=128)])
    new_password = PasswordField("New password", validators=[InputRequired(), Length(min=8, max=128)])
    confirm = PasswordField("Confirm new password", validators=[InputRequired(), EqualTo("new_password")])


class BlacklistForm(FlaskForm):
    customer_id = IntegerField("Customer ID", validators=[InputRequired(), NumberRange(min=1, max=2147483647)])
    reason = TextAreaField("Reason", filters=[trim], validators=[DataRequired(), Length(max=240)])


class ReasonForm(FlaskForm):
    reason = TextAreaField("Reason", filters=[trim], validators=[DataRequired(), Length(max=240)])


class CancelForm(ReasonForm):
    source = SelectField(
        "Cancellation type",
        choices=[SelectChoice("exception", "Late exception"), SelectChoice("hotel", "Hotel-initiated cancellation")],
        validators=[InputRequired()],
    )
