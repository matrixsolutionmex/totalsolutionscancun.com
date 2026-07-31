import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.notification import WebPushSubscription
from app.models.user import User
from app.models.user_lifecycle import UserLifecycleEvent
from app.core.auth_security import revoke_sessions_for_user


def normalize_user_status(status: str | None) -> str:
    value = (status or "").strip().upper()
    if value in {"PENDING_EMAIL", "PENDING_APPROVAL"}:
        return "PENDING"
    return value or "ACTIVE"


def revoke_user_access(db: Session, user: User, *, deactivate_push: bool = True) -> None:
    user.session_version = int(user.session_version or 0) + 1
    user.last_seen_at = None
    revoke_sessions_for_user(db, user.id, "user_access_revoked")
    if deactivate_push:
        (
            db.query(WebPushSubscription)
            .filter(WebPushSubscription.user_id == user.id, WebPushSubscription.active.is_(True))
            .update(
                {
                    WebPushSubscription.active: False,
                    WebPushSubscription.disabled_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )


def record_user_lifecycle_event(
    db: Session,
    *,
    user: User,
    actor: User | None,
    event_type: str,
    from_status: str | None,
    to_status: str,
    reason: str | None,
    metadata: dict | None = None,
) -> UserLifecycleEvent:
    event = UserLifecycleEvent(
        user_id=user.id,
        actor_user_id=actor.id if actor else None,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
    )
    db.add(event)
    return event


def transition_user_status(
    db: Session,
    *,
    user: User,
    actor: User | None,
    to_status: str,
    reason: str,
    event_type: str,
    is_active: bool,
    metadata: dict | None = None,
    deactivate_push: bool = True,
) -> UserLifecycleEvent:
    now = datetime.utcnow()
    previous_status = user.status
    user.status = to_status
    user.is_active = is_active
    user.status_reason = reason
    user.status_changed_at = now
    user.status_changed_by = actor.id if actor else None
    if to_status == "ARCHIVED":
        user.archived_at = now
    if to_status == "ANONYMIZED":
        user.anonymized_at = now
    revoke_user_access(db, user, deactivate_push=deactivate_push)
    return record_user_lifecycle_event(
        db,
        user=user,
        actor=actor,
        event_type=event_type,
        from_status=previous_status,
        to_status=to_status,
        reason=reason,
        metadata=metadata,
    )
