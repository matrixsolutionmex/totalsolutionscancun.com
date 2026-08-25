from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models import service_order_tracking  # noqa: F401 - registers ServiceOrderTracking mapper
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.organization_marketplace_link import OrganizationMarketplaceLink
from app.models.service_property import ServiceProperty
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.pricing_engine_service import seed_default_pricing_rates
from app.services.marketplace_service import create_opportunity_from_service_request
from app.services.organization_marketplace_service import (
    create_marketplace_link,
    ensure_default_marketplace_link,
    list_organization_marketplace_links,
    resolve_marketplace_link,
)
from app.routes.public_service_request_routes import public_marketplace, public_marketplace_campaign
from app.routes.admin_routes import MarketplaceLinkCreateRequest, admin_create_marketplace_link
from app.routes.public_service_request_routes import create_public_service_request


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def make_org(db, name):
    organization = Organization(name=name, slug=name.lower().replace(" ", "-"), status="ACTIVE")
    db.add(organization)
    db.flush()
    return organization


def test_each_organization_gets_one_idempotent_default_link(db):
    organization = make_org(db, "Empresa A")
    first = ensure_default_marketplace_link(db, organization)
    second = ensure_default_marketplace_link(db, organization)
    db.commit()

    assert first.id == second.id
    assert db.query(OrganizationMarketplaceLink).filter_by(organization_id=organization.id).count() == 1
    payload = public_marketplace(organization.slug, db)
    assert payload["organization_slug"] == organization.slug
    assert payload["link"]["slug"] == "default"


def test_admin_link_payload_contains_own_organization_slug_for_each_tenant(db):
    organization_a = make_org(db, "Global Express")
    organization_a.slug = "global-solutions-goiania"
    organization_b = make_org(db, "Otra Empresa")
    organization_b.slug = "otra-empresa"
    ensure_default_marketplace_link(db, organization_a)
    campaign = create_marketplace_link(db, organization_id=organization_a.id, name="Super Promo", slug="super-promo")
    ensure_default_marketplace_link(db, organization_b)
    db.commit()

    rows = list_organization_marketplace_links(db, organization_a.id)
    by_slug = {row["slug"]: row for row in rows}
    assert by_slug["default"]["organization_slug"] == "global-solutions-goiania"
    assert by_slug["super-promo"]["organization_slug"] == "global-solutions-goiania"
    assert campaign.slug == "super-promo"

    other_rows = list_organization_marketplace_links(db, organization_b.id)
    assert other_rows[0]["organization_slug"] == "otra-empresa"


def test_public_default_and_campaign_urls_resolve_by_organization_slug(db):
    organization = make_org(db, "Global Express")
    organization.slug = "global-solutions-goiania"
    default_link = ensure_default_marketplace_link(db, organization)
    campaign = create_marketplace_link(db, organization_id=organization.id, name="Super Promo", slug="super-promo")
    db.commit()

    assert public_marketplace(organization.slug, db)["link"]["slug"] == default_link.slug
    assert public_marketplace_campaign(organization.slug, campaign.slug, db)["link"]["slug"] == "super-promo"
    with pytest.raises(HTTPException) as error:
        public_marketplace("slug-inexistente", db)
    assert error.value.status_code == 404


def test_frontend_does_not_render_marketplace_url_without_organization_slug():
    html = (Path(__file__).parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'typeof link.organization_slug === "string"' in html
    assert "No disponible: la organización no tiene un slug público." in html


def test_campaigns_are_tenant_scoped_and_inactive_links_are_not_public(db):
    organization_a = make_org(db, "Empresa A")
    organization_b = make_org(db, "Empresa B")
    ensure_default_marketplace_link(db, organization_a)
    ensure_default_marketplace_link(db, organization_b)
    campaign = create_marketplace_link(
        db, organization_id=organization_a.id, name="Plomería", slug="plomeria", service_category="Hidraulica"
    )
    db.commit()

    resolved_org, resolved_link = resolve_marketplace_link(db, organization_a.slug, "plomeria")
    assert resolved_org.id == organization_a.id
    assert resolved_link.id == campaign.id
    with pytest.raises(HTTPException) as error:
        resolve_marketplace_link(db, organization_b.slug, "plomeria")
    assert error.value.status_code == 404

    campaign.active = False
    db.commit()
    with pytest.raises(HTTPException) as error:
        resolve_marketplace_link(db, organization_a.slug, "plomeria")
    assert error.value.status_code == 404


def test_campaign_attribution_is_persisted_on_request_and_opportunity(db):
    organization = make_org(db, "Empresa A")
    campaign = create_marketplace_link(db, organization_id=organization.id, name="Instalación", slug="instalacion")
    lead = Lead(organization_id=organization.id, nome="Cliente A", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(lead)
    db.flush()
    property_record = ServiceProperty(
        organization_id=organization.id, lead_id=lead.id, profile_type="CASA",
        address_line1="Calle 1", country_code="MX", locality="Cancún",
    )
    db.add(property_record)
    db.flush()
    request = ServiceRequest(
        organization_id=organization.id, marketplace_link_id=campaign.id,
        lead_id=lead.id, property_id=property_record.id, tracking_token="token-campaign-1",
        service_category="Hidraulica", requester_name="Cliente A", source="MARKETPLACE_LINK",
    )
    db.add(request)
    db.flush()
    opportunity = create_opportunity_from_service_request(db, request)
    db.commit()

    assert request.organization_id == organization.id
    assert request.marketplace_link_id == campaign.id
    assert opportunity.organization_id == organization.id
    assert opportunity.marketplace_link_id == campaign.id


def test_manager_cannot_create_campaign_for_another_organization_but_root_can(db):
    organization_a = make_org(db, "Empresa A")
    organization_b = make_org(db, "Empresa B")
    manager = User(username="manager-a", email="manager-a@example.com", password_hash="hash", full_name="Manager A", role="GERENTE", organization_id=organization_a.id, status="ACTIVE", is_active=True, email_verified=True)
    root = User(username="root", email="root@example.com", password_hash="hash", full_name="Root", role="ROOT", organization_id=organization_a.id, status="ACTIVE", is_active=True, email_verified=True)
    db.add_all([manager, root])
    db.flush()
    payload = MarketplaceLinkCreateRequest(organization_id=organization_b.id, name="B campaign", slug="b-campaign")

    with pytest.raises(HTTPException) as error:
        admin_create_marketplace_link(payload, db, manager)
    assert error.value.status_code == 403

    created = admin_create_marketplace_link(payload, db, root)
    assert created["organization_id"] == organization_b.id


def test_public_submission_resolves_organization_from_marketplace_slug(db):
    organization_a = make_org(db, "Empresa A")
    organization_b = make_org(db, "Empresa B")
    link = ensure_default_marketplace_link(db, organization_a)
    ensure_default_marketplace_link(db, organization_b)
    seed_default_pricing_rates(db)
    db.commit()

    response = create_public_service_request(
        requester_name="Cliente A", requester_phone="+525555000001", requester_email=None,
        property_type="Casa", service_category="Hidraulica", problem_description="Fuga en cocina",
        urgency="NORMAL", address_line1="Calle 1", address_line2=None, district=None,
        locality="Cancún", administrative_area="Quintana Roo", country_code="MX", postal_code=None,
        google_maps_url=None, latitude=None, longitude=None, location_lat=None, location_lng=None,
        location_accuracy_m=None, location_source=None, location_confirmed=False, preferred_visit_at=None,
        access_instructions=None, consent_privacy=True, consent_images=False, idempotency_key="link-a-1",
        public_language="es", customer_budget_min=None, customer_budget_max=None, pricing_zone=None,
        files=None, db=db, accept_language=None, organization_slug=organization_a.slug,
        marketplace_link_slug=link.slug,
    )
    request = db.query(ServiceRequest).filter_by(tracking_token=response["tracking_token"]).one()
    assert request.organization_id == organization_a.id
    assert request.marketplace_link_id == link.id
    assert db.query(ServiceRequest).filter(ServiceRequest.organization_id == organization_b.id).count() == 0
