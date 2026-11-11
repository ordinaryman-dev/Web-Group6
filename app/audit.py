from .models import AuditLog


def record_audit(session, actor_user_id, action, object_type, object_id, *, details=None, reason=None):
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            details=details,
            reason=reason,
        )
    )
