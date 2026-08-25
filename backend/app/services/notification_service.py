import json
import logging
import os
import secrets
import smtplib
from urllib import error as urlerror
from urllib import request as urlrequest
from datetime import datetime, timedelta
from email.message import EmailMessage

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.notification import EmailOutbox, Notification, NotificationPreference, WebPushSubscription
from app.models.organization_invitation import OrganizationInvitation
from app.core.auth_security import hash_value
from app.models.service_order import ServiceOrder
from app.models.user import User

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - production installs the optional push dependency.
    WebPushException = Exception
    webpush = None


logger = logging.getLogger(__name__)


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

    user = db.query(User).filter(User.id == user_id).first()
    preferences = NotificationPreference(user_id=user_id, organization_id=user.organization_id if user else None)
    db.add(preferences)
    db.flush()
    return preferences


def service_order_number(db: Session, lead_id: int) -> str:
    order = db.query(ServiceOrder).filter(ServiceOrder.lead_id == lead_id).first()
    return order.order_number if order and order.order_number else f"Cliente #{lead_id}"


def role_label(role: str | None) -> str:
    labels = {
        "ROOT": "Administrador",
        "GERENTE": "Supervisor",
        "BROKER": "Técnico",
    }
    return labels.get((role or "").upper(), "Usuario")


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
    allow_actor_recipient: bool = False,
) -> Notification | None:
    if not recipient or not recipient.is_active:
        return None
    if lead and recipient.organization_id != lead.organization_id:
        return None
    if actor and recipient.organization_id != actor.organization_id:
        return None
    if actor and actor.id == recipient.id and not allow_actor_recipient:
        return None

    existing = db.query(Notification).filter(Notification.idempotency_key == idempotency_key).first()
    if existing:
        return existing

    notification = Notification(
        organization_id=lead.organization_id if lead else recipient.organization_id,
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


def notify_user_activation(
    db: Session,
    *,
    activated_user: User,
    actor: User | None,
) -> list[int]:
    if not activated_user.organization_id or activated_user.status != "ACTIVE" or not activated_user.is_active:
        return []

    event_type = "TECHNICIAN_ACTIVATED" if activated_user.role == "BROKER" else "USER_ACTIVATED"
    display_name = activated_user.full_name or activated_user.username or "Usuario"
    if event_type == "TECHNICIAN_ACTIVATED":
        title = "Nuevo técnico registrado"
        message = f"{display_name} fue activado en el equipo técnico."
    else:
        title = "Nuevo usuario activado"
        message = f"{display_name} se incorporó como {role_label(activated_user.role)}."

    admins = (
        db.query(User)
        .filter(
            User.organization_id == activated_user.organization_id,
            User.role == "ROOT",
            User.status == "ACTIVE",
            User.is_active.is_(True),
        )
        .order_by(User.id.asc())
        .all()
    )
    event_key = (
        f"admin_event:{event_type}:org:{activated_user.organization_id}:"
        f"user:{activated_user.id}:activated:{activated_user.status_changed_at or activated_user.registered_at}"
    )
    eligible_admins = [admin for admin in admins if admin.id != activated_user.id]
    non_actor_admins = [admin for admin in eligible_admins if not actor or admin.id != actor.id]
    recipients = non_actor_admins or eligible_admins
    notification_ids: list[int] = []
    for admin in recipients:
        notification = create_notification(
            db,
            recipient=admin,
            actor=actor,
            type_=event_type.lower(),
            title=title,
            message=message,
            priority="NORMAL",
            action_url=f"/?admin_user_id={activated_user.id}",
            idempotency_key=f"{event_key}:recipient:{admin.id}",
            metadata={
                "event_type": event_type,
                "entity_type": "user",
                "entity_id": activated_user.id,
                "deduplication_key": event_key,
                "role": activated_user.role,
            },
            enqueue_email=False,
            allow_actor_recipient=not non_actor_admins,
        )
        if notification:
            notification_ids.append(notification.id)

    return notification_ids


def notification_push_payload(db: Session, notification: Notification) -> dict:
    lead = db.query(Lead).filter(Lead.id == notification.lead_id).first() if notification.lead_id else None
    order_number = service_order_number(db, lead.id) if lead else None
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    action_url = notification.action_url or (f"/?lead_id={lead.id}" if lead else "/")
    absolute_url = action_url
    if action_url.startswith("/") and base_url:
        absolute_url = f"{base_url}{action_url}"

    push_body = notification.message
    if lead and notification.type in {"lead_assigned", "lead_reassigned", "lead_unassigned"}:
        actor = actor_name(db.query(User).filter(User.id == notification.actor_user_id).first()) if notification.actor_user_id else "Sistema"
        if notification.type == "lead_unassigned":
            push_body = f"{actor} retiró la solicitud {order_number} de su responsabilidad."
        elif notification.type == "lead_reassigned":
            push_body = f"{actor} reasignó la solicitud {order_number}."
        else:
            push_body = f"{actor} asignó la solicitud {order_number} a su responsabilidad."

    return {
        "title": notification.title,
        "body": push_body,
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
        logger.info(
            "Web Push nao configurado: library=%s public_key=%s private_key=%s contact=%s",
            bool(webpush),
            bool(public_key),
            bool(private_key),
            bool(contact_email),
        )
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
    attempted = 0
    invalidated = 0
    failed = 0

    for notification in notifications:
        preferences = get_or_create_preferences(db, notification.recipient_user_id)
        if not preferences.browser_enabled:
            logger.info(
                "Web Push ignorado por preferencia desativada: notification_id=%s user_id=%s",
                notification.id,
                notification.recipient_user_id,
            )
            continue

        payload = notification_push_payload(db, notification)
        subscriptions = (
            db.query(WebPushSubscription)
            .filter(
                WebPushSubscription.user_id == notification.recipient_user_id,
                WebPushSubscription.organization_id == notification.organization_id,
                WebPushSubscription.active.is_(True),
            )
            .all()
        )
        if not subscriptions:
            logger.info(
                "Web Push sem assinatura ativa: notification_id=%s user_id=%s",
                notification.id,
                notification.recipient_user_id,
            )

        for subscription in subscriptions:
            attempted += 1
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
                    ttl=3600,
                    timeout=15,
                )
                subscription.last_used_at = datetime.utcnow()
                delivered += 1
            except WebPushException as exc:
                if _web_push_status_code(exc) in {404, 410}:
                    subscription.active = False
                    subscription.disabled_at = datetime.utcnow()
                    invalidated += 1
                else:
                    failed += 1
                    logger.warning(
                        "Web Push falhou: notification_id=%s user_id=%s status=%s error=%s",
                        notification.id,
                        notification.recipient_user_id,
                        _web_push_status_code(exc),
                        exc.__class__.__name__,
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Web Push falhou antes do provedor: notification_id=%s user_id=%s error=%s",
                    notification.id,
                    notification.recipient_user_id,
                    exc.__class__.__name__,
                )
                continue

    db.commit()
    if attempted or delivered or invalidated or failed:
        logger.info(
            "Web Push resultado: notifications=%s attempted=%s delivered=%s invalidated=%s failed=%s",
            len(notifications),
            attempted,
            delivered,
            invalidated,
            failed,
        )
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
        organization_id=notification.organization_id or recipient.organization_id,
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


def enqueue_invitation_email(db: Session, *, invitation: OrganizationInvitation, organization_name: str) -> EmailOutbox:
    existing = db.query(EmailOutbox).filter(EmailOutbox.invitation_id == invitation.id).first()
    if existing:
        return existing
    outbox = EmailOutbox(
        organization_id=invitation.organization_id,
        invitation_id=invitation.id,
        recipient_user_id=None,
        to_email=invitation.invited_email,
        subject=f"Convite para entrar em {organization_name}",
        body_text="",
        status="PENDING",
        provider="RESEND" if _resend_api_key() else "SMTP",
        idempotency_key=f"organization_invitation:{invitation.id}",
        next_attempt_at=datetime.utcnow(),
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
        if user.organization_id == lead.organization_id
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


def notify_client_created(
    db: Session,
    *,
    lead: Lead,
    actor: User | None,
) -> list[int]:
    if not lead.organization_id:
        return []

    admins = (
        db.query(User)
        .filter(
            User.organization_id == lead.organization_id,
            User.role == "ROOT",
            User.status == "ACTIVE",
            User.is_active.is_(True),
        )
        .order_by(User.id.asc())
        .all()
    )
    if not admins:
        return []

    deduplication_key = f"client_created:{lead.organization_id}:{lead.id}"
    actor_display_name = actor_name(actor)
    title = "Nuevo cliente registrado"
    message = f"{actor_display_name} registró un nuevo cliente."
    action_url = f"/?lead_id={lead.id}&source=client_created"
    eligible_admins = [admin for admin in admins if not actor or admin.id != actor.id]
    recipients = eligible_admins or admins
    notification_ids: list[int] = []

    for admin in recipients:
        is_actor_recipient = actor is not None and admin.id == actor.id
        notification = create_notification(
            db,
            recipient=admin,
            actor=actor,
            type_="client_created",
            title=title,
            message=message,
            lead=lead,
            priority="NORMAL",
            action_url=action_url,
            idempotency_key=f"{deduplication_key}:recipient:{admin.id}",
            metadata={
                "event_type": "CLIENT_CREATED",
                "entity_type": "client",
                "entity_id": lead.id,
                "client_id": lead.id,
                "deduplication_key": deduplication_key,
                "deep_link": action_url,
            },
            enqueue_email=False,
            allow_actor_recipient=not eligible_admins,
        )
        if notification and not is_actor_recipient:
            notification_ids.append(notification.id)

    return notification_ids


def _resend_api_key() -> str:
    explicit = os.getenv("RESEND_API_KEY", "").strip()
    if explicit:
        return explicit
    if os.getenv("SMTP_HOST", "").strip().lower() == "smtp.resend.com":
        return os.getenv("SMTP_PASSWORD", "").strip()
    return ""


def _prepare_invitation_email(db: Session, item: EmailOutbox) -> None:
    invitation = db.query(OrganizationInvitation).filter(
        OrganizationInvitation.id == item.invitation_id,
    ).with_for_update().first()
    if not invitation or invitation.status != "PENDING":
        raise RuntimeError("invitation_not_pending")
    raw_token = secrets.token_urlsafe(32)
    invitation.token_hash = hash_value(raw_token)
    invitation.expires_at = datetime.utcnow() + timedelta(days=7)
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    invite_url = f"{base_url}/invite/{raw_token}" if base_url else f"/invite/{raw_token}"
    item.body_text = (
        "Você foi convidado para entrar em uma organização Total Solutions.\n\n"
        f"Abra o convite: {invite_url}\n\n"
        "O convite expira em 7 dias e pode ser usado uma única vez."
    )


def _send_outbox_with_resend(item: EmailOutbox, *, api_key: str, sender: str) -> str | None:
    payload = {"from": sender, "to": [item.to_email], "subject": item.subject, "text": item.body_text}
    request = urlrequest.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": item.idempotency_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "TotalSolutionsCRM/1.0 (+https://totalsolutionscancun.com)",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            if not 200 <= response.status < 300:
                raise RuntimeError(f"resend_http_{response.status}")
            try:
                return str(json.loads(body).get("id", ""))[:80] or None
            except json.JSONDecodeError:
                return None
    except urlerror.HTTPError as exc:
        raise RuntimeError(f"resend_http_{getattr(exc, 'code', 'unknown')}") from exc
    except (urlerror.URLError, TimeoutError) as exc:
        raise RuntimeError(f"resend_{exc.__class__.__name__}") from exc


def _send_outbox_with_smtp(item: EmailOutbox, *, host: str, sender: str) -> None:
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = item.to_email
    message["Subject"] = item.subject
    if host.lower() == "smtp.resend.com":
        message["Resend-Idempotency-Key"] = item.idempotency_key
    message.set_content(item.body_text)
    smtp_factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_factory(host, port, timeout=15) as smtp:
        if use_tls and not use_ssl:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)


EMAIL_OUTBOX_LEASE_SECONDS = 120
EMAIL_OUTBOX_MAX_ATTEMPTS = 5


def _claim_next_email_outbox(db: Session) -> EmailOutbox | None:
    now = datetime.utcnow()
    lease_expired_at = now - timedelta(seconds=EMAIL_OUTBOX_LEASE_SECONDS)
    item = (
        db.query(EmailOutbox)
        .filter(
            or_(
                (EmailOutbox.status.in_(["PENDING", "RETRY"]) & (EmailOutbox.next_attempt_at <= now)),
                (EmailOutbox.status == "PROCESSING") & (EmailOutbox.claimed_at <= lease_expired_at),
            )
        )
        .order_by(EmailOutbox.next_attempt_at.asc(), EmailOutbox.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if not item:
        db.rollback()
        return None

    item.status = "PROCESSING"
    item.claimed_at = now
    item.attempts = (item.attempts or 0) + 1
    item.last_attempt_at = now
    db.commit()
    return item


def process_email_outbox(db: Session, *, limit: int = 10) -> int:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    resend_key = _resend_api_key()
    if not smtp_from or (not resend_key and not smtp_host):
        return 0

    sent = 0

    for _ in range(limit):
        item = _claim_next_email_outbox(db)
        if not item:
            break
        try:
            if item.invitation_id:
                _prepare_invitation_email(db, item)
            if resend_key:
                item.provider = "RESEND"
                item.provider_message_id = _send_outbox_with_resend(item, api_key=resend_key, sender=smtp_from)
            else:
                item.provider = "SMTP"
                _send_outbox_with_smtp(item, host=smtp_host, sender=smtp_from)
            item.status = "SENT"
            item.sent_at = datetime.utcnow()
            item.claimed_at = None
            item.last_error = None
            if item.invitation_id:
                item.body_text = ""
            sent += 1
        except Exception as exc:  # noqa: BLE001 - delivery must not break application work.
            item.status = "FAILED" if item.attempts >= EMAIL_OUTBOX_MAX_ATTEMPTS else "RETRY"
            item.last_error = (str(exc)[:160] if isinstance(exc, RuntimeError) else exc.__class__.__name__)
            if item.invitation_id:
                item.body_text = ""
            item.claimed_at = None
            item.next_attempt_at = datetime.utcnow() + timedelta(minutes=min(30, 2 ** item.attempts))
        db.commit()

    return sent
