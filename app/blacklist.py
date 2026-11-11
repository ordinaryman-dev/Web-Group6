from sqlalchemy import or_

from .extensions import db
from .models import BlacklistEntry


def customer_is_blocked(session, customer_id, at):
    """Call after locking the Customer row in a booking transaction."""
    return session.scalar(
        db.select(BlacklistEntry.id)
        .where(
            BlacklistEntry.customer_id == customer_id,
            BlacklistEntry.effective_at <= at,
            or_(BlacklistEntry.removed_at.is_(None), BlacklistEntry.removed_at > at),
        )
        .limit(1)
    ) is not None
