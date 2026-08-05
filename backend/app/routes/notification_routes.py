import json
import os
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.models.notification import Notification, WebPushSubscription
from app.models.user import User
from app.schemas.notification_schema import (
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    NotificationResponse,
    WebPushDeactivateRequest,
    WebPushStateResponse,
    WebPushSubscriptionCreate,
    WebPushSubscriptionResponse,
)
from app.services.notification_service import get_or_create_preferences

router = APIRouter(tags=["notifications"])


def notification_payload(db: Session, notification: Notification) -> NotificationResponse:
    actor = db.query(User).filter(User.id == notification.actor_user_id).first() if notification.actor_user_id else None
    metadata = None
    if notification.metadata_json:
        try:
            metadata = json.loads(notification.metadata_json)
        except json.JSONDecodeError:
            metadata = None

    return NotificationResponse(
        id=notification.id,
        recipient_user_id=notification.recipient_user_id,
        actor_user_id=notification.actor_user_id,
        actor_name=(actor.full_name or actor.username) if actor else None,
        type=notification.type,
        title=notification.title,
        message=notification.message,
        lead_id=notification.lead_id,
        priority=notification.priority,
        action_url=notification.action_url,
        read_at=notification.read_at,
        created_at=notification.created_at,
        metadata=metadata,
    )


@router.get("/notifications", response_model=list[NotificationResponse])
def list_notifications(
    limit: int = Query(default=20, ge=1, le=100),
    after_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    query = db.query(Notification).filter(
        Notification.recipient_user_id == actor.id,
        Notification.organization_id == actor.organization_id,
    )
    if after_id:
        query = query.filter(Notification.id > after_id)

    notifications = (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .all()
    )
    return [notification_payload(db, notification) for notification in notifications]


@router.get("/notifications/unread-count")
def unread_notification_count(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    count = (
        db.query(func.count(Notification.id))
        .filter(
            Notification.recipient_user_id == actor.id,
            Notification.organization_id == actor.organization_id,
            Notification.read_at.is_(None),
        )
        .scalar()
    )
    return {"unread": int(count or 0)}


@router.patch("/notifications/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    notification = (
        db.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.recipient_user_id == actor.id,
            Notification.organization_id == actor.organization_id,
        )
        .first()
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notificacao nao encontrada")

    if not notification.read_at:
        notification.read_at = func.now()
        db.commit()
        db.refresh(notification)

    return notification_payload(db, notification)


@router.post("/notifications/read-all")
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    updated = (
        db.query(Notification)
        .filter(
            Notification.recipient_user_id == actor.id,
            Notification.organization_id == actor.organization_id,
            Notification.read_at.is_(None),
        )
        .update({Notification.read_at: func.now()}, synchronize_session=False)
    )
    db.commit()
    return {"updated": updated}


@router.get("/notifications/stream")
def stream_notifications(
    after_id: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    actor_id = actor.id

    def event_stream():
        last_id = after_id
        deadline = time.time() + 25
        while time.time() < deadline:
            session = next(get_db())
            try:
                rows = (
                    session.query(Notification)
                    .filter(
                        Notification.recipient_user_id == actor_id,
                        Notification.organization_id == actor.organization_id,
                        Notification.id > last_id,
                    )
                    .order_by(Notification.id.asc())
                    .limit(20)
                    .all()
                )
                if rows:
                    for row in rows:
                        last_id = max(last_id, row.id)
                        payload = notification_payload(session, row).model_dump(mode="json")
                        yield f"id: {row.id}\nevent: notification\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    return
            finally:
                session.close()

            time.sleep(2)

        yield ": keepalive\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/notification-preferences", response_model=NotificationPreferenceResponse)
def get_notification_preferences(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    preferences = get_or_create_preferences(db, actor.id)
    db.commit()
    return NotificationPreferenceResponse.model_validate(preferences, from_attributes=True)


@router.patch("/notification-preferences", response_model=NotificationPreferenceResponse)
def update_notification_preferences(
    payload: NotificationPreferenceUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    preferences = get_or_create_preferences(db, actor.id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(preferences, key, value)
    db.commit()
    db.refresh(preferences)
    return NotificationPreferenceResponse.model_validate(preferences, from_attributes=True)


@router.post("/notification-preferences/test-sound")
def test_notification_sound(actor: User = Depends(get_actor)):
    return {"ok": True, "message": "Reproduza o som no navegador"}


def web_push_subscription_payload(subscription: WebPushSubscription) -> WebPushSubscriptionResponse:
    return WebPushSubscriptionResponse(
        id=subscription.id,
        device_label=subscription.device_label,
        active=subscription.active,
        created_at=subscription.created_at,
        last_used_at=subscription.last_used_at,
    )


@router.get("/web-push/state", response_model=WebPushStateResponse)
def get_web_push_state(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    vapid_public_key = os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY", "").strip()
    subscriptions = (
        db.query(WebPushSubscription)
        .filter(
            WebPushSubscription.user_id == actor.id,
            WebPushSubscription.organization_id == actor.organization_id,
            WebPushSubscription.active.is_(True),
        )
        .order_by(WebPushSubscription.created_at.desc(), WebPushSubscription.id.desc())
        .all()
    )
    return WebPushStateResponse(
        supported=bool(vapid_public_key),
        subscribed=bool(subscriptions),
        vapid_public_key=vapid_public_key or None,
        subscriptions=[web_push_subscription_payload(subscription) for subscription in subscriptions],
    )


@router.post("/web-push/subscriptions", response_model=WebPushSubscriptionResponse)
def register_web_push_subscription(
    payload: WebPushSubscriptionCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
    user_agent: str | None = Header(default=None),
):
    endpoint = payload.endpoint.strip()
    p256dh = payload.keys.p256dh.strip()
    auth = payload.keys.auth.strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Assinatura web push incompleta")

    subscription = db.query(WebPushSubscription).filter(WebPushSubscription.endpoint == endpoint).first()
    if not subscription:
        subscription = WebPushSubscription(endpoint=endpoint, user_id=actor.id)
        db.add(subscription)

    subscription.user_id = actor.id
    subscription.organization_id = actor.organization_id
    subscription.p256dh = p256dh
    subscription.auth = auth
    subscription.device_label = payload.device_label or "Este dispositivo"
    subscription.user_agent = user_agent
    subscription.active = True
    subscription.disabled_at = None

    preferences = get_or_create_preferences(db, actor.id)
    preferences.browser_enabled = True
    db.commit()
    db.refresh(subscription)
    return web_push_subscription_payload(subscription)


@router.post("/web-push/subscriptions/deactivate")
def deactivate_web_push_subscription(
    payload: WebPushDeactivateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    subscription = (
        db.query(WebPushSubscription)
        .filter(WebPushSubscription.endpoint == payload.endpoint, WebPushSubscription.user_id == actor.id)
        .first()
    )
    if not subscription:
        return {"deactivated": False}

    subscription.active = False
    subscription.disabled_at = func.now()
    db.commit()
    return {"deactivated": True}


@router.delete("/web-push/subscriptions/{subscription_id}")
def deactivate_web_push_subscription_by_id(
    subscription_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    subscription = (
        db.query(WebPushSubscription)
        .filter(WebPushSubscription.id == subscription_id, WebPushSubscription.user_id == actor.id)
        .first()
    )
    if not subscription:
        raise HTTPException(status_code=404, detail="Dispositivo nao encontrado")

    subscription.active = False
    subscription.disabled_at = func.now()
    db.commit()
    return {"deactivated": True}
