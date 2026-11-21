# 员工表单：定义付款、退款、到店入住、维修及审批需要填写的字段；业务状态仍由 desk 再次检查。
from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    DateField, DecimalField, HiddenField, IntegerField, SelectChoice,
    SelectField, StringField, SubmitField, TimeField,
)
from wtforms.validators import InputRequired, Length, NumberRange, Optional
from wtforms.widgets import HiddenInput


class PaymentForm(FlaskForm):
    amount = DecimalField("Amount", validators=[Optional(), NumberRange(min=Decimal("0.01"))])
    idempotency_key = HiddenField()
    submit = SubmitField("Record payment")


class RefundForm(FlaskForm):
    amount = DecimalField("Refund", validators=[Optional(), NumberRange(min=Decimal("0.01"))])
    idempotency_key = HiddenField()
    submit = SubmitField("Record refund")


class WalkInForm(FlaskForm):
    customer_id = IntegerField(widget=HiddenInput(), validators=[Optional(), NumberRange(min=1)])
    room_id = IntegerField(widget=HiddenInput(), validators=[Optional()])
    name = StringField("Guest name", validators=[Optional(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=40)])
    room_type_id = SelectField("Room type", coerce=int, validators=[InputRequired()])
    check_in = DateField("Check-in", validators=[InputRequired()])
    check_out = DateField("Check-out", validators=[InputRequired()])
    guests = IntegerField("Guests", validators=[InputRequired(), NumberRange(min=1, max=6)])
    note = StringField("Note (optional)", validators=[Optional(), Length(max=100)])
    submit = SubmitField("Create order and check in")


class BlockForm(FlaskForm):
    room_id = IntegerField("Room", validators=[InputRequired()], widget=HiddenInput())
    start_date = DateField("From", validators=[InputRequired()])
    end_date = DateField("To", validators=[InputRequired()])
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Set out of service")


class ReassignForm(FlaskForm):
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Move to a free room")


class DecisionForm(FlaskForm):
    comment = StringField("Decision note", validators=[Optional(), Length(max=240)])
    submit = SubmitField("Save")


class ContactForm(FlaskForm):
    result = SelectField(
        "Contact result",
        choices=[
            SelectChoice("", "Choose a result"),
            SelectChoice("reached_late", "Reached - arriving later"),
            SelectChoice("reached_not_coming", "Reached - not coming"),
            SelectChoice("no_answer", "No answer"),
            SelectChoice("wrong_number", "Wrong number"),
        ],
        validators=[InputRequired()],
    )
    new_expected = TimeField("New expected arrival", validators=[Optional()])
    note = StringField("Note (optional)", validators=[Optional(), Length(max=240)])
    submit = SubmitField("Save contact")


class NoShowForm(FlaskForm):
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Mark no-show and release")


class CancelBookingForm(FlaskForm):
    reason = StringField("Reason", validators=[InputRequired(), Length(max=240)])
    submit = SubmitField("Cancel with manager authority")
