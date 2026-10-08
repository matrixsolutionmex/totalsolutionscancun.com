from datetime import datetime, timedelta
import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.customer_portal import CustomerClaimToken, CustomerServiceLink
from app.models.organization import Organization
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.import_service import normalize_email, normalize_phone


def customer_invitation_config() -> dict:
    enabled = os.getenv("CUSTOMER_PORTAL_INVITATIONS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    mode = os.getenv("CUSTOMER_PORTAL_INVITATIONS_ROLLOUT_MODE", "off").strip().lower()
    raw_ids = os.getenv("CUSTOMER_PORTAL_INVITATIONS_CANARY_ORGANIZATION_IDS", "").strip()
    if mode not in {"off", "canary", "all"} or (mode != "canary" and raw_ids):
        return {"enabled": False, "valid": False}
    ids = set()
    if mode == "canary":
        if not raw_ids:
            return {"enabled": False, "valid": False}
        for value in raw_ids.split(","):
            value = value.strip()
            if not value.isascii() or not value.isdigit() or int(value) <= 0:
                return {"enabled": False, "valid": False}
            ids.add(int(value))
        if len(ids) != len([value for value in raw_ids.split(",") if value.strip()]):
            return {"enabled": False, "valid": False}
    return {"enabled": enabled, "valid": True, "mode": mode, "organization_ids": frozenset(ids)}


def customer_invitations_available(db: Session, organization_id: int | None) -> bool:
    config = customer_invitation_config()
    if not config.get("enabled") or not config.get("valid") or config.get("mode") == "off":
        return False
    if config["mode"] == "canary" and organization_id not in config["organization_ids"]:
        return False
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    return bool(organization and (organization.status or "").upper() == "ACTIVE")


def customer_portal_config() -> dict:
    enabled = os.getenv("CUSTOMER_PORTAL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    mode = os.getenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "off").strip().lower()
    raw_ids = os.getenv("CUSTOMER_PORTAL_CANARY_ORGANIZATION_IDS", "").strip()
    if mode not in {"off", "canary", "all"}:
        return {"enabled": False, "valid": False}
    if mode != "canary" and raw_ids:
        return {"enabled": False, "valid": False}
    ids: set[int] = set()
    if mode == "canary":
        if not raw_ids:
            return {"enabled": False, "valid": False}
        for value in raw_ids.split(","):
            value = value.strip()
            if not value.isascii() or not value.isdigit() or int(value) <= 0:
                return {"enabled": False, "valid": False}
            ids.add(int(value))
        if len(ids) != len([value for value in raw_ids.split(",") if value.strip()]):
            return {"enabled": False, "valid": False}
    return {"enabled": enabled, "valid": True, "mode": mode, "organization_ids": frozenset(ids)}


def customer_portal_available(db: Session, organization_id: int | None) -> bool:
    config = customer_portal_config()
    if not config.get("enabled") or not config.get("valid") or config.get("mode") == "off":
        return False
    if config["mode"] == "canary" and organization_id not in config["organization_ids"]:
        return False
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    return bool(organization and (organization.status or "").upper() == "ACTIVE")


def require_customer(user: User, db: Session) -> None:
    if user.role != "CLIENTE" or not user.is_active or user.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Acesso de cliente não autorizado")
    if not customer_portal_available(db, user.organization_id):
        raise HTTPException(status_code=404, detail="Painel do cliente indisponível")


def _claim_secret() -> bytes:
    secret = os.getenv("CUSTOMER_PORTAL_CLAIM_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Vinculo de cliente não configurado")
    return secret.encode()


def _hash_token(token: str) -> str:
    return hmac.new(_claim_secret(), token.encode(), hashlib.sha256).hexdigest()


def create_customer_claim_token(db: Session, service_request: ServiceRequest, channel: str) -> str:
    if channel not in {"email", "phone"}:
        raise HTTPException(status_code=400, detail="Canal de verificação inválido")
    token = secrets.token_urlsafe(32)
    db.add(CustomerClaimToken(
        organization_id=service_request.organization_id,
        service_request_id=service_request.id,
        token_hash=_hash_token(token),
        channel=channel,
        expires_at=datetime.utcnow() + timedelta(minutes=30),
    ))
    return token


def verify_customer_claim(db: Session, token: str, user: User) -> CustomerServiceLink:
    if user.role != "CLIENTE" or not user.is_active or user.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Conta de cliente não autorizada")
    record = db.query(CustomerClaimToken).filter(
        CustomerClaimToken.token_hash == _hash_token(token),
        CustomerClaimToken.used_at.is_(None),
        CustomerClaimToken.revoked_at.is_(None),
    ).with_for_update().first()
    if not record or record.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Código de vinculação inválido ou expirado")
    request = db.query(ServiceRequest).filter(
        ServiceRequest.id == record.service_request_id,
        ServiceRequest.organization_id == record.organization_id,
    ).first()
    if not request or user.organization_id != request.organization_id:
        raise HTTPException(status_code=400, detail="Código de vinculação inválido")
    if record.channel == "email":
        if not user.email_verified or normalize_email(user.email) != normalize_email(request.requester_email):
            raise HTTPException(status_code=403, detail="Canal de verificação não corresponde")
    else:
        if normalize_phone(user.telefone) != normalize_phone(request.requester_phone):
            raise HTTPException(status_code=403, detail="Canal de verificação não corresponde")
    link = db.query(CustomerServiceLink).filter_by(
        customer_user_id=user.id, service_request_id=request.id,
    ).first()
    if link and link.active:
        record.used_at = datetime.utcnow()
        return link
    link = link or CustomerServiceLink(
        organization_id=request.organization_id,
        customer_user_id=user.id,
        service_request_id=request.id,
        verification_method=record.channel,
    )
    link.active = True
    link.revoked_at = None
    db.add(link)
    record.used_at = datetime.utcnow()
    return link
