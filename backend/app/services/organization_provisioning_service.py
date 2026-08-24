"""Transactional provisioning of an empty organization tenant."""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.commercial_subscription import CommercialSubscription
from app.models.organization import Organization
from app.models.organization_invitation import OrganizationInvitation
from app.models.user import User
from app.services.entitlement_service import PLANS, normalize_plan
from app.services.organization_onboarding_service import create_invitation
from app.services.localization_service import normalize_language


SUPPORTED_CURRENCIES = {"MXN", "USD", "BRL", "GBP", "CAD"}


def normalize_slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", (value or "").strip().lower())
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        raise HTTPException(status_code=422, detail="Informe um nome ou slug válido para a organização")
    return text[:100]


def _validate_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="E-mail do gerente inválido")
    return email


def provision_organization(
    db: Session,
    *,
    name: str,
    slug: str | None,
    country: str,
    language: str,
    currency: str,
    timezone: str,
    plan: str,
    manager_full_name: str,
    manager_email: str,
    invited_by: User,
):
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="Nome da organização é obrigatório")
    clean_slug = normalize_slug(slug or clean_name)
    if db.query(Organization).filter(Organization.slug == clean_slug).first():
        raise HTTPException(status_code=409, detail="Slug de organização já utilizado")

    email = _validate_email(manager_email)
    existing_user = db.query(User).filter(User.email.ilike(email)).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="O e-mail do gerente já pertence a um usuário")
    existing_invitation = db.query(OrganizationInvitation).filter(
        OrganizationInvitation.invited_email == email,
        OrganizationInvitation.status == "PENDING",
    ).first()
    if existing_invitation:
        raise HTTPException(status_code=409, detail="Já existe um convite pendente para este e-mail")

    normalized_plan = normalize_plan(plan)
    if normalized_plan not in PLANS:
        raise HTTPException(status_code=422, detail="Plano inválido")
    normalized_language = normalize_language(language)
    normalized_currency = (currency or "").strip().upper()
    if normalized_currency not in SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Moeda não suportada")
    clean_country = (country or "").strip().upper()
    if len(clean_country) != 2 or not clean_country.isalpha():
        raise HTTPException(status_code=422, detail="País deve usar código ISO de duas letras")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError):
        raise HTTPException(status_code=422, detail="Timezone inválida")

    organization = Organization(
        name=clean_name,
        slug=clean_slug,
        country=clean_country,
        language=normalized_language,
        currency=normalized_currency,
        timezone=timezone,
        plan=normalized_plan,
        # The tenant is operationally available, but remains empty until the
        # invited manager completes registration, email verification and approval.
        status="ACTIVE",
        is_platform_owner=False,
    )
    db.add(organization)
    db.flush()

    # The subscription is configuration, not operational data. Pricing rates remain global
    # until this tenant explicitly creates an organization override.
    db.add(CommercialSubscription(
        organization_id=organization.id,
        plan=normalized_plan,
        status="LAUNCH_ACCESS",
        provider="MOCK",
        reference_price=PLANS[normalized_plan]["price"],
    ))
    invitation, raw_token = create_invitation(
        db,
        organization=organization,
        invited_by=invited_by,
        invited_email=email,
        role="GERENTE",
    )
    db.flush()
    return organization, invitation, raw_token


def provision_response(organization: Organization, invitation: OrganizationInvitation, *, email_delivery_status: str, warnings: list[str]) -> dict:
    return {
        "organization_id": organization.id,
        "organization_name": organization.name,
        "slug": organization.slug,
        "invitation_id": invitation.id,
        "manager_email": invitation.invited_email,
        "status": organization.status,
        "invitation_status": invitation.status,
        "email_delivery_status": email_delivery_status,
        "created_at": organization.created_at.isoformat() if isinstance(organization.created_at, datetime) else organization.created_at,
        "warnings": warnings,
    }
