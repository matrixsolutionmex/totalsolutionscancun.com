import re
import os
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.user import User
from app.models.commercial_subscription import CommercialSubscription
from app.services.entitlement_service import PLANS


DEFAULT_ORGANIZATION_SLUG = "total-solutions-cancun"


def primary_organization_slug() -> str:
    return os.getenv("PLATFORM_PRIMARY_ORGANIZATION_SLUG", DEFAULT_ORGANIZATION_SLUG).strip().lower() or DEFAULT_ORGANIZATION_SLUG


def get_platform_primary_organization(db: Session) -> Organization:
    organization = db.query(Organization).filter(Organization.slug == primary_organization_slug()).first()
    if not organization and os.getenv("ENVIRONMENT", "").strip().lower() != "production":
        return get_or_create_default_organization(db)
    if not organization:
        raise RuntimeError(f"Organização principal da plataforma não encontrada: {primary_organization_slug()}")
    return organization


def get_or_create_default_organization(db: Session) -> Organization:
    Organization.__table__.create(bind=db.get_bind(), checkfirst=True)
    slug = primary_organization_slug()
    organization = db.query(Organization).filter(Organization.slug == slug).first()
    if organization:
        return organization

    if os.getenv("ENVIRONMENT", "").strip().lower() == "production" and os.getenv("PLATFORM_ALLOW_PRIMARY_BOOTSTRAP", "false").strip().lower() != "true":
        raise RuntimeError(f"Organização principal da plataforma não encontrada: {slug}")

    organization = Organization(
        name="Total Solutions Cancún",
        slug=slug,
        industry_profile="SERVICIOS_TECNICOS",
        country="MX",
        language="es-MX",
        currency="MXN",
        timezone="America/Cancun",
        date_format="DD/MM/YYYY",
        plan="FREE",
        status="ACTIVE",
        is_platform_owner=True,
    )
    db.add(organization)
    db.flush()
    return organization


def create_independent_organization(
    db: Session,
    *,
    name: str,
    country: str = "MX",
    pending_onboarding: bool = True,
) -> Organization:
    """Create a private FREE workspace; callers must explicitly opt into invitations."""
    CommercialSubscription.__table__.create(bind=db.get_bind(), checkfirst=True)
    clean_name = (name or "Minha organização").strip()[:160] or "Minha organização"
    base = re.sub(r"[^a-z0-9]+", "-", clean_name.lower()).strip("-") or "workspace"
    slug = f"{base}-{uuid4().hex[:10]}"
    organization = Organization(
        name=clean_name,
        slug=slug,
        country=(country or "MX").upper()[:2],
        plan="FREE",
        status="PENDING_ONBOARDING" if pending_onboarding else "ACTIVE",
        is_platform_owner=False,
    )
    db.add(organization)
    db.flush()
    db.add(
        CommercialSubscription(
            organization_id=organization.id,
            plan="FREE",
            status="LAUNCH_ACCESS",
            provider="MOCK",
            reference_price=PLANS["FREE"]["price"],
        )
    )
    db.flush()
    return organization


def actor_organization_id(actor: User | None) -> int | None:
    return actor.organization_id if actor else None


def organization_filter(model, actor: User | None):
    organization_id = actor_organization_id(actor)
    if not organization_id or not hasattr(model, "organization_id"):
        return True
    return model.organization_id == organization_id
