from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.user import User


DEFAULT_ORGANIZATION_SLUG = "total-solutions-cancun"


def get_or_create_default_organization(db: Session) -> Organization:
    Organization.__table__.create(bind=db.get_bind(), checkfirst=True)
    organization = db.query(Organization).filter(Organization.slug == DEFAULT_ORGANIZATION_SLUG).first()
    if organization:
        return organization

    organization = Organization(
        name="Total Solutions Cancún",
        slug=DEFAULT_ORGANIZATION_SLUG,
        industry_profile="SERVICIOS_TECNICOS",
        country="MX",
        language="es-MX",
        currency="MXN",
        timezone="America/Cancun",
        date_format="DD/MM/YYYY",
        plan="INTERNAL",
        status="ACTIVE",
        is_platform_owner=True,
    )
    db.add(organization)
    db.flush()
    return organization


def actor_organization_id(actor: User | None) -> int | None:
    return actor.organization_id if actor else None


def organization_filter(model, actor: User | None):
    organization_id = actor_organization_id(actor)
    if not organization_id or not hasattr(model, "organization_id"):
        return True
    return model.organization_id == organization_id
