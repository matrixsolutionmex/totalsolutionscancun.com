"""Tenant-scoped public marketplace links and attribution."""

import re
import unicodedata

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.organization_marketplace_link import OrganizationMarketplaceLink


def normalize_marketplace_link_slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", (value or "").strip().lower())
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        raise HTTPException(status_code=422, detail="Slug da campanha inválido")
    return text[:120]


def ensure_default_marketplace_link(db: Session, organization: Organization) -> OrganizationMarketplaceLink:
    link = db.query(OrganizationMarketplaceLink).filter(
        OrganizationMarketplaceLink.organization_id == organization.id,
        OrganizationMarketplaceLink.slug == "default",
    ).first()
    if link:
        return link
    link = OrganizationMarketplaceLink(
        organization_id=organization.id,
        name="Marketplace principal",
        slug="default",
        source_code="MARKETPLACE_LINK",
        visibility_scope="ORGANIZATION",
        active=True,
    )
    db.add(link)
    db.flush()
    return link


def resolve_marketplace_link(db: Session, organization_slug: str, link_slug: str = "default") -> tuple[Organization, OrganizationMarketplaceLink]:
    organization = db.query(Organization).filter(
        Organization.slug == organization_slug,
        Organization.status == "ACTIVE",
    ).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Marketplace não encontrado")
    link = db.query(OrganizationMarketplaceLink).filter(
        OrganizationMarketplaceLink.organization_id == organization.id,
        OrganizationMarketplaceLink.slug == normalize_marketplace_link_slug(link_slug),
        OrganizationMarketplaceLink.active.is_(True),
        OrganizationMarketplaceLink.visibility_scope == "ORGANIZATION",
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link Marketplace não encontrado")
    return organization, link


def public_marketplace_payload(organization: Organization, link: OrganizationMarketplaceLink, services: list[str] | None = None) -> dict:
    return {
        "organization_name": organization.name,
        "organization_slug": organization.slug,
        "organization_slug": organization.slug,
        "language": organization.language,
        "currency": organization.currency,
        "services": services or [],
        "link": {
            "name": link.name,
            "slug": link.slug,
            "service_category": link.service_category,
            "campaign_name": link.campaign_name,
            "source": link.source_code,
            "active": bool(link.active),
        },
    }


def list_organization_marketplace_links(db: Session, organization_id: int) -> list[dict]:
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organização não encontrada")
    links = db.query(OrganizationMarketplaceLink).filter(
        OrganizationMarketplaceLink.organization_id == organization_id,
    ).order_by(OrganizationMarketplaceLink.slug).all()
    return [{
        "id": link.id,
        "organization_id": link.organization_id,
        "name": link.name,
        "slug": link.slug,
        "service_category": link.service_category,
        "campaign_name": link.campaign_name,
        "source_code": link.source_code,
        "visibility_scope": link.visibility_scope,
        "active": bool(link.active),
        "organization_name": organization.name,
    } for link in links]


def create_marketplace_link(db: Session, *, organization_id: int, name: str, slug: str,
                            service_category: str | None = None, active: bool = True) -> OrganizationMarketplaceLink:
    clean_slug = normalize_marketplace_link_slug(slug)
    if clean_slug == "default":
        raise HTTPException(status_code=409, detail="O link principal já é reservado")
    if db.query(OrganizationMarketplaceLink).filter(
        OrganizationMarketplaceLink.organization_id == organization_id,
        OrganizationMarketplaceLink.slug == clean_slug,
    ).first():
        raise HTTPException(status_code=409, detail="Slug de campanha já utilizado nesta organização")
    link = OrganizationMarketplaceLink(
        organization_id=organization_id,
        name=(name or "").strip()[:160] or clean_slug,
        slug=clean_slug,
        service_category=(service_category or "").strip()[:120] or None,
        campaign_name=(name or "").strip()[:160] or clean_slug,
        source_code="MARKETPLACE_LINK",
        visibility_scope="ORGANIZATION",
        active=bool(active),
    )
    db.add(link)
    db.flush()
    return link
