"""Nightly pricing for one stay.

This module is the single pricing entry point for the whole application.
Search, the public ``/api/search`` endpoint, the booking allocation and the
front-desk re-quote all call :func:`quote`, so a night is never priced twice by
two different code paths.

The caller owns the transaction: every function here takes the caller's session
and never commits, rolls back or opens a connection of its own. The booking
module calls :func:`quote` inside its allocation lock, so no locking happens
here either.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .extensions import db
from .models import PriceRule, RoomType

INVALID_REQUEST = "INVALID_REQUEST"
NOT_FOUND = "NOT_FOUND"


class PricingError(Exception):
    """A stay that cannot be priced.

    ``code`` lets callers map the failure onto a business error without
    inspecting the message text.
    """

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class NightPrice:
    """One night of a stay, and the rule that produced the amount."""

    stay_date: object
    amount: Decimal
    rule_id: int | None


@dataclass(frozen=True)
class Rate:
    """Consecutive nights that share a price, used for display and signing."""

    start: object
    end: object
    amount: Decimal
    rule_id: int | None


def rule_precedence(rule):
    """Order overlapping rules deterministically.

    A ``date`` rule beats a ``season`` rule. Between two rules of the same kind
    the later ``start_date`` wins, so a narrow holiday season overrides the
    longer season around it. The id only breaks exact ties, which keeps the
    result stable across query order.
    """

    return (rule.kind == "date", rule.start_date, rule.id)


def stay_rules(session, room_type_id, check_in, check_out):
    """Fetch every live rule that can touch the stay, in one query."""

    return session.scalars(
        db.select(PriceRule).where(
            PriceRule.room_type_id == room_type_id,
            PriceRule.active.is_(True),
            PriceRule.start_date < check_out,
            PriceRule.end_date > check_in,
        )
    ).all()


def quote(session, room_type_id, check_in, check_out):
    """Return one :class:`NightPrice` per night of ``[check_in, check_out)``.

    Falls back to ``room_types.base_price`` with ``rule_id=None`` when no rule
    covers a night. Raises :class:`PricingError` when the stay is empty or the
    room type does not exist.
    """

    if check_out <= check_in:
        raise PricingError(INVALID_REQUEST, "Check-out must be after check-in.")
    room_type = session.get(RoomType, room_type_id)
    if room_type is None:
        raise PricingError(NOT_FOUND, "Room type not found.")

    rules = stay_rules(session, room_type_id, check_in, check_out)
    prices = []
    day = check_in
    while day < check_out:
        matches = [rule for rule in rules if rule.start_date <= day < rule.end_date]
        best = max(matches, key=rule_precedence, default=None)
        if best is None:
            prices.append(NightPrice(day, room_type.base_price, None))
        else:
            prices.append(NightPrice(day, best.amount, best.id))
        day += timedelta(days=1)
    return prices


def total(prices):
    """Sum nightly amounts as a two-decimal money value."""

    return sum((price.amount for price in prices), Decimal("0.00")).quantize(Decimal("0.01"))


def group_rates(prices):
    """Collapse a run of equal nightly prices into display segments."""

    rates = []
    for price in prices:
        previous = rates[-1] if rates else None
        if previous and (previous.amount, previous.rule_id) == (price.amount, price.rule_id):
            rates[-1] = Rate(previous.start, price.stay_date + timedelta(days=1), price.amount, price.rule_id)
        else:
            rates.append(Rate(price.stay_date, price.stay_date + timedelta(days=1), price.amount, price.rule_id))
    return rates
