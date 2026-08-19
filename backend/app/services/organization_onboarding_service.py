import secrets
import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.auth_security import hash_value
from app.core.organization import create_independent_organization
from app.models.organization import Organization
from app.models.organization_invitation import OrganizationInvitation
from app.models.referral_attribution import ReferralAttribution
from app.models.user import User

INVITATION_TTL_DAYS = 7
INVITATION_ROLES = {"BROKER", "GERENTE"}
logger = logging.getLogger(__name__)


def create_invitation(
    db: Session,
    *,
    organization: Organization,
    invited_by: User,
    invited_email: str,
    role: str = "BROKER",
    supervisor_user_id: int | None = None,
) -> tuple[OrganizationInvitation, str]:
    OrganizationInvitation.__table__.create(bind=db.get_bind(), checkfirst=True)
    email = invited_email.strip().lower()
    role = role.strip().upper()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="E-mail do convite inválido")
    if role not in INVITATION_ROLES:
        raise HTTPException(status_code=400, detail="Role de convite inválida")
    if supervisor_user_id is not None:
        supervisor = db.query(User).filter(
            User.id == supervisor_user_id,
            User.organization_id == organization.id,
            User.role == "GERENTE",
            User.is_active.is_(True),
        ).first()
        if not supervisor:
            raise HTTPException(status_code=400, detail="Supervisor fora da organização ou inválido")
    raw_token = secrets.token_urlsafe(32)
    invitation = OrganizationInvitation(
        organization_id=organization.id,
        invited_email=email,
        invited_by_user_id=invited_by.id,
        role=role,
        supervisor_user_id=supervisor_user_id,
        token_hash=hash_value(raw_token),
        status="PENDING",
        expires_at=datetime.utcnow() + timedelta(days=INVITATION_TTL_DAYS),
    )
    db.add(invitation)
    db.flush()
    return invitation, raw_token


def invitation_for_token(db: Session, raw_token: str, *, lock: bool = False) -> OrganizationInvitation:
    token = (raw_token or "").strip()
    invitation = db.query(OrganizationInvitation).filter(
        OrganizationInvitation.token_hash == hash_value(token)
    )
    if lock:
        invitation = invitation.with_for_update()
    invitation = invitation.first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Convite inválido ou expirado")
    if invitation.status != "PENDING":
        raise HTTPException(status_code=409, detail="Convite já utilizado ou cancelado")
    if invitation.expires_at <= datetime.utcnow():
        invitation.status = "EXPIRED"
        db.commit()
        raise HTTPException(status_code=410, detail="Convite expirado")
    return invitation


def accept_invitation(db: Session, *, raw_token: str, user: User) -> OrganizationInvitation:
    invitation = invitation_for_token(db, raw_token, lock=True)
    if (user.email or user.username or "").strip().lower() != invitation.invited_email:
        raise HTTPException(status_code=403, detail="O convite foi enviado para outro e-mail")
    if user.organization_id and user.organization_id != invitation.organization_id:
        raise HTTPException(status_code=409, detail="Usuário já pertence a outra organização")
    user.organization_id = invitation.organization_id
    user.role = invitation.role
    user.manager_id = invitation.supervisor_user_id
    user.onboarding_source = "TEAM"
    invitation.status = "ACCEPTED"
    invitation.accepted_at = datetime.utcnow()
    db.flush()
    return invitation


def record_referral(
    db: Session,
    *,
    user: User,
    referral_code: str | None = None,
    referral_email: str | None = None,
    invitation_id: int | None = None,
) -> ReferralAttribution | None:
    if not any((referral_code, referral_email, invitation_id)):
        return None
    OrganizationInvitation.__table__.create(bind=db.get_bind(), checkfirst=True)
    ReferralAttribution.__table__.create(bind=db.get_bind(), checkfirst=True)
    attribution = ReferralAttribution(
        user_id=user.id,
        source="INVITATION" if invitation_id else "PUBLIC_SIGNUP",
        referral_code=(referral_code or "").strip()[:120] or None,
        referral_email=(referral_email or "").strip().lower()[:320] or None,
        invitation_id=invitation_id,
    )
    db.add(attribution)
    db.flush()
    return attribution


def send_invitation_email(*, to_email: str, organization_name: str, role: str, invite_url: str) -> bool:
    """Send only the invitation link; the raw token is never persisted or logged."""
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if not host or not sender:
        return False
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_email
    message["Subject"] = f"Convite para entrar em {organization_name}"
    message.set_content(
        f"Você foi convidado para entrar em {organization_name} como {role}.\n\n"
        f"Abra o convite: {invite_url}\n\nO convite expira em {INVITATION_TTL_DAYS} dias."
    )
    try:
        use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() == "true"
        use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
        port = int(os.getenv("SMTP_PORT", "465" if use_ssl else "587"))
        smtp_factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_factory(host, port, timeout=15) as smtp:
            if use_tls and not use_ssl:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001 - invitation creation must remain available if SMTP is down.
        logger.warning("Falha ao enviar convite; tipo=%s", exc.__class__.__name__)
        return False
