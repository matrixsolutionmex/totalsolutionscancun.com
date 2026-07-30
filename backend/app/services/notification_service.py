import json
import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.notification import EmailOutbox, Notification, NotificationPreference, WebPushSubscription
from app.models.service_order import ServiceOrder
from app.models.user import User

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - production installs the optional push dependency.
    WebPushException = Exception
    webpush = None


def actor_name(actor: User | None) -> str:
    if not actor:
        return "Sistema"
    return actor.full_name or actor.username


def user_email(user: User | None) -> str | None:
    if not user:
        return None
    return user.email or user.email_pessoal


def get_or_create_preferences(db: Session, user_id: int) -> NotificationPreference:
    preferences = db.query(NotificationPreference).filter(NotificationPreference.user_id == user_id).first()
    if preferences:
        return preferences

    preferences = NotificationPreference(user_id=user_id)
    db.add(preferences)
    db.flush()
    return preferences


def service_order_number(db: Session, lead_id: int) -> str:
    order = db.query(ServiceOrder).filter(ServiceOrder.lead_id == lead_id).first()
    return order.order_number if order and order.order_number else f"Cliente #{lead_id}"


def create_notification(
    db: Session,
    *,
    recipient: User | None,
    actor: User | None,
    type_: str,
    title: str,
    message: str,
    lead: Lead | None = None,
    priority: str = "NORMAL",
    action_url: str | None = None,
    idempotency_key: str,
    metadata: dict | None = None,
    enqueue_email: bool = False,
) -> Notification | None:
    if not recipient or not recipient.is_active:
        return None
    if actor and actor.id == recipient.id:
        return None

    existing = db.query(Notification).filter(Notification.idempotency_key == idempotency_key).first()
    if existing:
        return existing

    notification = Notification(
        recipient_user_id=recipient.id,
        actor_user_id=actor.id if actor else None,
        type=type_,
        title=title[:255],
        message=message,
        lead_id=lead.id if lead else None,
        priority=priority or "NORMAL",
        action_url=action_url,
        idempotency_key=idempotency_key,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False) if metadata else None,
    )
    db.add(notification)
    db.flush()

    if enqueue_email:
        enqueue_notification_email(db, notification, recipient, actor, lead)

    return notification


def notification_push_payload(db: Session, notification: Notification) -> dict:
    lead = db.query(Lead).filter(Lead.id == notification.lead_id).first() if notification.lead_id else None
    order_number = service_order_number(db, lead.id) if lead else None
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    action_url = notification.action_url or (f"/?lead_id={lead.id}" if lead else "/")
    absolute_url = action_url
    if action_url.startswith("/") and base_url:
        absolute_url = f"{base_url}{action_url}"

    return {
        "title": notification.title,
        "body": notification.message,
        "url": absolute_url,
        "lead_id": notification.lead_id,
        "notification_id": notification.id,
        "priority": notification.priority,
        "order_number": order_number,
        "tag": f"ts-notification-{notification.id}",
    }


def _web_push_ready() -> tuple[str, str, str] | None:
    public_key = os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY", "").strip()
    private_key = os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
    contact_email = os.getenv("WEB_PUSH_CONTACT_EMAIL", "").strip()
    if not webpush or not public_key or not private_key or not contact_email:
        return None
    return public_key, private_key, contact_email


def _web_push_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(response, "status", None)
    return int(status_code) if status_code else None


def dispatch_web_push_for_notification_ids(db: Session, notification_ids: list[int] | None) -> int:
    if not notification_ids:
        return 0

    vapid = _web_push_ready()
    if not vapid:
        return 0

    _, private_key, contact_email = vapid
    notifications = (
        db.query(Notification)
        .filter(Notification.id.in_(notification_ids))
        .order_by(Notification.id.asc())
        .all()
    )
    delivered = 0

    for notification in notifications:
        preferences = get_or_create_preferences(db, notification.recipient_user_id)
        if not preferences.browser_enabled:
            continue

        payload = notification_push_payload(db, notification)
        subscriptions = (
            db.query(WebPushSubscription)
            .filter(
                WebPushSubscription.user_id == notification.recipient_user_id,
                WebPushSubscription.active.is_(True),
            )
            .all()
        )

        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info={
                        "endpoint": subscription.endpoint,
                        "keys": {
                            "p256dh": subscription.p256dh,
                            "auth": subscription.auth,
                        },
                    },
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=private_key,
                    vapid_claims={"sub": f"mailto:{contact_email}"},
                )
                subscription.last_used_at = datetime.utcnow()
                delivered += 1
            except WebPushException as exc:
                if _web_push_status_code(exc) in {404, 410}:
                    subscription.active = False
                    subscription.disabled_at = datetime.utcnow()
            except Exception:
                continue

    db.commit()
    return delivered


def enqueue_notification_email(
    db: Session,
    notification: Notification,
    recipient: User,
    actor: User | None,
    lead: Lead | None,
) -> EmailOutbox | None:
    preferences = get_or_create_preferences(db, recipient.id)
    to_email = user_email(recipient)
    if not preferences.email_enabled or not to_email:
        return None

    lead_label = service_order_number(db, lead.id) if lead else "Total Solutions"
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    action_url = notification.action_url or (f"{base_url}/?lead_id={lead.id}" if base_url and lead else "")
    subject = f"Total Solutions - nueva solicitud asignada ({lead_label})"
    body = "\n".join(
        [
            f"Hola {recipient.full_name or recipient.username},",
            "",
            "Tiene una nueva solicitud asignada en Total Solutions.",
            f"OS/Cliente: {lead_label}",
            f"Cliente: {lead.nome if lead else 'No informado'}",
            f"Servicio: {lead.tipo_servico or lead.nicho if lead else 'No informado'}",
            f"Prioridad: {lead.urgencia or notification.priority if lead else notification.priority}",
            f"Asignado por: {actor_name(actor)}",
            "",
            f"Abrir en Total Solutions: {action_url or 'Disponible dentro del sistema'}",
        ]
    )
    outbox = EmailOutbox(
        notification_id=notification.id,
        recipient_user_id=recipient.id,
        to_email=to_email,
        subject=subject,
        body_text=body,
        idempotency_key=f"email:{notification.idempotency_key}",
        next_attempt_at=datetime.utcnow() + timedelta(seconds=5),
    )
    db.add(outbox)
    return outbox


def notify_assignment_change(
    db: Session,
    *,
    lead: Lead,
    actor: User | None,
    previous_user_id: int | None,
    new_user_id: int | None,
):
    notification_ids: list[int] = []
    if previous_user_id == new_user_id:
        return notification_ids

    order_number = service_order_number(db, lead.id)
    action_url = f"/?lead_id={lead.id}"
    priority = "URGENTE" if (lead.urgencia or "").upper() in {"ALTA", "EMERGENCIA"} else "NORMAL"
    users_by_id = {
        user.id: user
        for user in db.query(User).filter(User.id.in_([uid for uid in {previous_user_id, new_user_id} if uid])).all()
    }

    if new_user_id:
        recipient = users_by_id.get(new_user_id)
        type_ = "lead_reassigned" if previous_user_id else "lead_assigned"
        title = "Nueva solicitud asignada" if type_ == "lead_assigned" else "Solicitud redistribuida"
        message = f"{actor_name(actor)} asigno {order_number} - {lead.nome or 'Cliente'} a su responsabilidad."
        notification = create_notification(
            db,
            recipient=recipient,
            actor=actor,
            type_=type_,
            title=title,
            message=message,
            lead=lead,
            priority=priority,
            action_url=action_url,
            idempotency_key=f"{type_}:{lead.id}:{previous_user_id or 'none'}:{new_user_id}:{lead.updated_at}",
            metadata={"order_number": order_number, "urgency": lead.urgencia},
            enqueue_email=True,
        )
        if notification:
            notification_ids.append(notification.id)

    if previous_user_id and previous_user_id != new_user_id:
        recipient = users_by_id.get(previous_user_id)
        message = f"{actor_name(actor)} retiro {order_number} - {lead.nome or 'Cliente'} de su responsabilidad."
        notification = create_notification(
            db,
            recipient=recipient,
            actor=actor,
            type_="lead_unassigned",
            title="Solicitud retirada",
            message=message,
            lead=lead,
            priority="NORMAL",
            action_url=action_url,
            idempotency_key=f"lead_unassigned:{lead.id}:{previous_user_id}:{new_user_id or 'none'}:{lead.updated_at}",
            metadata={"order_number": order_number},
            enqueue_email=False,
        )
        if notification:
            notification_ids.append(notification.id)

    return notification_ids


def process_email_outbox(db: Session, *, limit: int = 10) -> int:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    if not smtp_host or not smtp_from:
        return 0

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

    messages = (
        db.query(EmailOutbox)
        .filter(EmailOutbox.status.in_(["PENDING", "RETRY"]), EmailOutbox.next_attempt_at <= datetime.utcnow())
        .order_by(EmailOutbox.next_attempt_at.asc(), EmailOutbox.id.asc())
        .limit(limit)
        .all()
    )
    sent = 0

    for item in messages:
        try:
            message = EmailMessage()
            message["From"] = smtp_from
            message["To"] = item.to_email
            message["Subject"] = item.subject
            message.set_content(item.body_text)

            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
                if use_tls:
                    smtp.starttls()
                if smtp_username and smtp_password:
                    smtp.login(smtp_username, smtp_password)
                smtp.send_message(message)

            item.status = "SENT"
            item.sent_at = datetime.utcnow()
            item.last_error = None
            sent += 1
        except Exception as exc:  # noqa: BLE001 - avoid breaking assignments because of email delivery.
            item.attempts += 1
            item.status = "FAILED" if item.attempts >= 5 else "RETRY"
            item.last_error = exc.__class__.__name__
            item.next_attempt_at = datetime.utcnow() + timedelta(minutes=min(30, 2 ** item.attempts))

    if messages:
        db.commit()

    return sent
