import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.customer_invitation import CustomerPortalInvitation, CustomerPortalInvitationEvent
from app.models.notification import EmailOutbox
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.customer_account_service import customer_invitations_available
from app.services.import_service import normalize_email, normalize_phone


INVITATION_TTL_MINUTES = 30
CANONICAL_PUBLIC_ORIGIN = "https://totalsolutionscancun.com"
STAFF_ROLES = {"ROOT", "GERENTE", "BROKER", "TECNICO", "ADMIN"}


def _validated_public_origin() -> str:
    raw = os.getenv("PUBLIC_BASE_URL", "")
    if not raw or not raw.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise HTTPException(status_code=503, detail="Origem pública de convites não configurada")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Origem pública de convites não configurada") from exc

    if (
        raw not in {CANONICAL_PUBLIC_ORIGIN, f"{CANONICAL_PUBLIC_ORIGIN}/"}
        or parsed.scheme != "https"
        or parsed.hostname != "totalsolutionscancun.com"
        or parsed.netloc != "totalsolutionscancun.com"
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or port is not None
    ):
        raise HTTPException(status_code=503, detail="Origem pública de convites não configurada")
    return CANONICAL_PUBLIC_ORIGIN


def _activation_url(raw_token: str, *, origin: str) -> str:
    return f"{origin}/cliente/activar?token={quote(raw_token, safe='')}"


def _token_hash(raw: str) -> str:
    secret = os.getenv("CUSTOMER_PORTAL_CLAIM_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Convites de cliente não configurados")
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _contact(channel: str, request: ServiceRequest) -> tuple[str, str]:
    if channel == "EMAIL":
        value = normalize_email(request.requester_email or "")
        return value, f"{value[:2]}***@{value.split('@', 1)[1]}"
    if channel == "PHONE":
        value = normalize_phone(request.requester_phone or "")
        return value, f"***{value[-4:]}"
    raise HTTPException(status_code=400, detail="Canal inválido")


def _destination_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _event(db: Session, invitation: CustomerPortalInvitation, event_type: str, actor_id: int | None = None):
    db.add(CustomerPortalInvitationEvent(
        invitation_id=invitation.id,
        organization_id=invitation.organization_id,
        event_type=event_type,
        actor_user_id=actor_id,
        metadata_json=json.dumps({"channel": invitation.channel}, separators=(",", ":")),
    ))


def _active_invitation(db: Session, request_id: int, channel: str):
    return db.query(CustomerPortalInvitation).filter(
        CustomerPortalInvitation.service_request_id == request_id,
        CustomerPortalInvitation.channel == channel,
        CustomerPortalInvitation.status.in_(["QUEUED", "DELIVERED"]),
        CustomerPortalInvitation.revoked_at.is_(None),
        CustomerPortalInvitation.consumed_at.is_(None),
        CustomerPortalInvitation.expires_at > datetime.utcnow(),
    ).order_by(CustomerPortalInvitation.id.desc()).first()


def create_invitation(db: Session, *, actor: User, service_request_id: int, channel: str, language: str, idempotency_key: str):
    if not customer_invitations_available(db, actor.organization_id):
        raise HTTPException(status_code=404, detail="Convites de cliente indisponíveis")
    origin = _validated_public_origin()
    request = db.query(ServiceRequest).filter(ServiceRequest.id == service_request_id).first()
    if not request or (actor.role != "ROOT" and request.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    if (request.status or "").upper() in {"ARCHIVED", "ANONYMIZED", "DELETED"}:
        raise HTTPException(status_code=409, detail="Solicitação não elegível")
    normalized, masked = _contact(channel, request)
    users = db.query(User).filter(User.status.notin_(["ANONYMIZED", "ARCHIVED"])).all()
    same_channel = []
    for user in users:
        current = normalize_email(user.email or "") if channel == "EMAIL" and user.email else normalize_phone(user.telefone or "") if channel == "PHONE" and user.telefone else ""
        if current and current == normalized:
            same_channel.append(user)
    if len(same_channel) > 1 or any(user.role in STAFF_ROLES for user in same_channel if user.status == "ACTIVE" and user.is_active):
        raise HTTPException(status_code=409, detail="Canal possui conflito de identidade")
    existing = db.query(CustomerPortalInvitation).filter(CustomerPortalInvitation.idempotency_key == idempotency_key).first()
    if existing:
        return existing, None
    active = _active_invitation(db, request.id, channel)
    if active:
        raise HTTPException(status_code=409, detail="Já existe convite ativo")
    raw = secrets.token_urlsafe(32)
    invitation = CustomerPortalInvitation(
        organization_id=request.organization_id, service_request_id=request.id, channel=channel,
        destination_hash=_destination_hash(normalized), masked_destination=masked,
        token_hash=_token_hash(raw), language=(language or "es")[:12],
        status="QUEUED", expires_at=datetime.utcnow() + timedelta(minutes=INVITATION_TTL_MINUTES),
        idempotency_key=idempotency_key, created_by_user_id=actor.id,
    )
    db.add(invitation)
    db.flush()
    _event(db, invitation, "INVITATION_CREATED", actor.id)
    if channel == "EMAIL":
        url = _activation_url(raw, origin=origin)
        db.add(EmailOutbox(
            organization_id=request.organization_id, to_email=normalized,
            customer_portal_invitation_id=invitation.id,
            subject="Activa tu panel de cliente - Total Solutions",
            body_text=f"Activa tu cuenta en: {url}", body_html="", template_type="CUSTOMER_PORTAL_INVITATION",
            status="PENDING", provider="FAKE" if os.getenv("ENVIRONMENT") != "production" else "SMTP",
            idempotency_key=f"customer_portal_invitation:{invitation.id}", next_attempt_at=datetime.utcnow(),
        ))
    _event(db, invitation, "INVITATION_QUEUED", actor.id)
    return invitation, raw


def revoke_invitation(db: Session, invitation: CustomerPortalInvitation, actor: User):
    if invitation.organization_id != actor.organization_id and actor.role != "ROOT":
        raise HTTPException(status_code=404, detail="Convite não encontrado")
    if invitation.status in {"REVOKED", "CONSUMED"}:
        return invitation
    invitation.status = "REVOKED"
    invitation.revoked_at = datetime.utcnow()
    _event(db, invitation, "INVITATION_REVOKED", actor.id)
    return invitation


def resend_invitation(db: Session, invitation: CustomerPortalInvitation, actor: User, idempotency_key: str):
    if not customer_invitations_available(db, actor.organization_id):
        raise HTTPException(status_code=404, detail="Convites de cliente indisponíveis")
    _validated_public_origin()
    revoke_invitation(db, invitation, actor)
    _event(db, invitation, "INVITATION_REISSUED", actor.id)
    db.flush()
    return create_invitation(db, actor=actor, service_request_id=invitation.service_request_id, channel=invitation.channel, language=invitation.language, idempotency_key=idempotency_key)


def inspect_token(db: Session, raw_token: str):
    record = db.query(CustomerPortalInvitation).filter(CustomerPortalInvitation.token_hash == _token_hash(raw_token)).first()
    if not record or record.expires_at < datetime.utcnow() or record.revoked_at or record.consumed_at:
        return None
    return record


def consume_existing_customer_invitation(db: Session, raw_token: str, user: User):
    """Consume only a migration-094 invitation for an already logged-in client."""
    invitation = db.query(CustomerPortalInvitation).filter(
        CustomerPortalInvitation.token_hash == _token_hash(raw_token),
    ).with_for_update().first()
    if not invitation or invitation.expires_at < datetime.utcnow() or invitation.revoked_at or invitation.consumed_at:
        raise HTTPException(status_code=400, detail="Convite inválido, expirado ou já utilizado")
    if not customer_invitations_available(db, invitation.organization_id):
        raise HTTPException(status_code=404, detail="Convites de cliente indisponíveis")
    if user.role != "CLIENTE" or not user.is_active or user.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Conta de cliente não autorizada")
    if user.organization_id != invitation.organization_id:
        raise HTTPException(status_code=400, detail="Convite inválido")

    request = db.query(ServiceRequest).filter(
        ServiceRequest.id == invitation.service_request_id,
        ServiceRequest.organization_id == invitation.organization_id,
    ).first()
    if not request:
        raise HTTPException(status_code=400, detail="Convite inválido")
    destination = user.email if invitation.channel == "EMAIL" else user.telefone
    normalized = normalize_email(destination or "") if invitation.channel == "EMAIL" else normalize_phone(destination or "")
    if not normalized or _destination_hash(normalized) != invitation.destination_hash:
        raise HTTPException(status_code=403, detail="Canal de verificação não corresponde")
    if invitation.channel == "EMAIL" and not user.email_verified:
        raise HTTPException(status_code=403, detail="Canal de verificação não corresponde")

    from app.models.customer_portal import CustomerServiceLink
    link = db.query(CustomerServiceLink).filter(
        CustomerServiceLink.customer_user_id == user.id,
        CustomerServiceLink.service_request_id == request.id,
    ).first()
    if link and link.active and link.revoked_at is None:
        invitation.status = "CONSUMED"
        invitation.consumed_at = datetime.utcnow()
        _event(db, invitation, "CLAIM_CONSUMED", user.id)
        return link
    if link:
        link.active = True
        link.revoked_at = None
        link.verification_method = invitation.channel.lower()
    else:
        link = CustomerServiceLink(
            organization_id=invitation.organization_id,
            customer_user_id=user.id,
            service_request_id=request.id,
            verification_method=invitation.channel.lower(),
        )
        db.add(link)
    invitation.status = "CONSUMED"
    invitation.consumed_at = datetime.utcnow()
    _event(db, invitation, "CLAIM_CONSUMED", user.id)
    return link


def activate_new_customer(db: Session, raw_token: str, full_name: str, password: str, email: str | None, phone: str | None):
    invitation = inspect_token(db, raw_token)
    if not invitation:
        raise HTTPException(status_code=400, detail="Convite inválido ou expirado")
    destination = email if invitation.channel == "EMAIL" else phone
    normalized = normalize_email(destination or "") if invitation.channel == "EMAIL" else normalize_phone(destination or "")
    if _destination_hash(normalized) != invitation.destination_hash:
        raise HTTPException(status_code=403, detail="Canal não corresponde")
    existing = []
    for candidate in db.query(User).filter(User.status.notin_(["ANONYMIZED", "ARCHIVED"])).all():
        current = normalize_email(candidate.email or "") if invitation.channel == "EMAIL" and candidate.email else normalize_phone(candidate.telefone or "") if invitation.channel == "PHONE" and candidate.telefone else ""
        if current == normalized:
            existing.append(candidate)
    if existing:
        raise HTTPException(status_code=409, detail="Use o login da conta existente para concluir o convite")
    user = User(
        organization_id=invitation.organization_id, username=f"cliente-{secrets.token_hex(8)}",
        email=normalized if invitation.channel == "EMAIL" else None, telefone=normalized if invitation.channel == "PHONE" else None,
        password_hash=hash_password(password), full_name=full_name.strip(), role="CLIENTE", status="ACTIVE",
        is_active=True, email_verified=invitation.channel == "EMAIL", onboarding_source="CUSTOMER_PORTAL_INVITATION",
    )
    db.add(user)
    db.flush()
    from app.models.customer_portal import CustomerServiceLink
    db.add(CustomerServiceLink(organization_id=invitation.organization_id, customer_user_id=user.id, service_request_id=invitation.service_request_id, verification_method=invitation.channel.lower()))
    invitation.status = "CONSUMED"
    invitation.consumed_at = datetime.utcnow()
    _event(db, invitation, "CUSTOMER_ACCOUNT_CREATED")
    _event(db, invitation, "CLAIM_CONSUMED")
    return user
