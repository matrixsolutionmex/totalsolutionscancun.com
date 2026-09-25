import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.database.connection import Base
from app.main import app
from app.models.commercial_compliance import CommercialAuditEvent, CommercialOutreach, GlobalSuppression
from app.models.organization import Organization
from app.models.user import User
from app.routes.commercial_compliance_routes import (
    OutreachIn,
    PreferenceIn,
    SuppressionIn,
    _register_preference,
    approve_outreach_route,
    can_contact_route,
    create_outreach_route,
    create_suppression,
    list_outreach,
    list_suppressions,
)
from app.services.commercial_compliance_service import (
    PRIVACY_NOTICE_VERSION,
    can_contact,
    create_outreach,
    normalize_email,
    privacy_notice_status,
    public_opt_out,
    upsert_suppression,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def legal_env(monkeypatch):
    monkeypatch.setenv("LEGAL_ENTITY_NAME", "Total Solutions Test")
    monkeypatch.setenv("BUSINESS_ADDRESS", "QA Address")
    monkeypatch.setenv("PRIVACY_CONTACT_EMAIL", "privacy@example.test")
    monkeypatch.setenv("PRIVACY_CONTROLLER_NAME", "QA Controller")
    monkeypatch.setenv("PRIVACY_JURISDICTION", "MX")
    monkeypatch.setenv("PRIVACY_EFFECTIVE_DATE", "2026-01-01")


def org(db, slug="org"):
    row = Organization(name=slug.title(), slug=slug, status="ACTIVE")
    db.add(row)
    db.flush()
    return row


def actor(db, organization, role="GERENTE", username=None):
    row = User(
        organization_id=organization.id if organization else None,
        username=username or f"{role.lower()}-{organization.id if organization else 'global'}",
        email=f"{role.lower()}-{organization.id if organization else 'global'}@example.test",
        password_hash="x",
        role=role,
        full_name=role,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(row)
    db.flush()
    return row


def request(email="qa@example.test"):
    return Request({
        "type": "http", "method": "POST", "path": "/preferencias-comunicacion",
        "headers": [], "client": ("127.0.0.1", 8000), "scheme": "http", "server": ("test", 80),
    })


def get_page(path: str) -> httpx.Response:
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(run())


def test_privacy_public_pages_es_en_pt_are_reachable():
    es = get_page("/aviso-de-privacidad")
    en = get_page("/aviso-de-privacidad?lang=en")
    pt = get_page("/aviso-de-privacidad?lang=pt")

    assert es.status_code == 200 and "Aviso de Privacidad" in es.text
    assert en.status_code == 200 and "Privacy Notice" in en.text
    assert pt.status_code == 200 and "Aviso de Privacidade" in pt.text
    assert PRIVACY_NOTICE_VERSION in es.text


def test_communication_preferences_route_and_generic_response(db):
    page = get_page("/preferencias-comunicacion")
    assert page.status_code == 200
    assert "Preferencias de comunicación" in page.text

    existing = _register_preference(db, request(), PreferenceIn(email="Known@Example.com", action="STOP_MARKETING"))
    unknown = _register_preference(db, request(), PreferenceIn(email="unknown@example.invalid", action="STOP_MARKETING"))

    assert existing == unknown == {"message": "Su preferencia ha sido registrada."}


def test_email_normalization_and_invalid_values():
    assert normalize_email(" User@Example.COM ") == "user@example.com"
    assert normalize_email("bad address@example.com") is None


def test_public_opt_out_creates_email_suppression_and_audit(db):
    public_opt_out(db, email="Person@Example.com", action="STOP_MARKETING", reason="no gracias")
    db.commit()

    suppression = db.query(GlobalSuppression).one()
    assert suppression.normalized_email == "person@example.com"
    assert suppression.scope == "EMAIL_ONLY"
    assert suppression.reason == "OPT_OUT"
    assert db.query(CommercialAuditEvent).filter_by(event_type="opt_out_received").count() == 1
    assert db.query(CommercialAuditEvent).filter(CommercialAuditEvent.event_type.in_(["suppression_created", "suppression_updated"])).count() == 1


def test_individual_opt_out_does_not_create_domain_suppression(db):
    public_opt_out(db, email="owner@example.com", action="STOP_ALL_NON_TRANSACTIONAL")
    db.commit()

    row = db.query(GlobalSuppression).one()
    assert row.normalized_email == "owner@example.com"
    assert row.domain is None
    assert row.scope == "ALL_MARKETING"


def test_duplicate_opt_out_is_idempotent(db):
    public_opt_out(db, email="owner@example.com", action="STOP_MARKETING")
    public_opt_out(db, email="OWNER@example.com", action="STOP_MARKETING")
    db.commit()

    assert db.query(GlobalSuppression).count() == 1
    assert db.query(CommercialAuditEvent).filter_by(event_type="opt_out_received").count() == 2


def test_suppression_precedence_blocks_before_privacy_ready(db, monkeypatch):
    monkeypatch.delenv("PRIVACY_CONTACT_EMAIL", raising=False)
    organization = org(db)
    manager = actor(db, organization)
    upsert_suppression(db, organization_id=organization.id, email="blocked@example.com", actor_user_id=manager.id)
    outreach = create_outreach(db, actor=manager, payload={
        "company_name": "Blocked Co", "recipient": "blocked@example.com", "contact_source": "QA", "compliance_status": "VALID",
    })
    approve_outreach_route(outreach.id, db=db, actor=manager)

    assert can_contact(db, outreach_id=outreach.id).decision == "BLOCKED_SUPPRESSION"


def test_domain_suppression_blocks_entire_domain(db, legal_env):
    organization = org(db)
    manager = actor(db, organization)
    upsert_suppression(db, organization_id=organization.id, domain="example.com", scope="DOMAIN", actor_user_id=manager.id)
    outreach = create_outreach(db, actor=manager, payload={
        "company_name": "Domain Co", "recipient": "person@example.com", "contact_source": "QA", "compliance_status": "VALID",
    })
    approve_outreach_route(outreach.id, db=db, actor=manager)

    assert can_contact(db, outreach_id=outreach.id).decision == "BLOCKED_DOMAIN"


def test_privacy_not_ready_blocks_without_placeholders(db, monkeypatch):
    for key in ("LEGAL_ENTITY_NAME", "BUSINESS_ADDRESS", "PRIVACY_CONTACT_EMAIL", "PRIVACY_CONTROLLER_NAME", "PRIVACY_JURISDICTION", "PRIVACY_EFFECTIVE_DATE"):
        monkeypatch.delenv(key, raising=False)
    organization = org(db)
    manager = actor(db, organization)
    outreach = create_outreach(db, actor=manager, payload={
        "company_name": "Privacy Co", "recipient": "person@privacy.test", "contact_source": "QA", "compliance_status": "VALID",
    })
    approve_outreach_route(outreach.id, db=db, actor=manager)

    assert privacy_notice_status()["ready"] is False
    assert can_contact(db, outreach_id=outreach.id).decision == "BLOCKED_PRIVACY_NOT_READY"


def test_can_contact_requires_compliance_and_human_approval(db, legal_env):
    organization = org(db)
    manager = actor(db, organization)
    outreach = create_outreach(db, actor=manager, payload={
        "company_name": "Review Co", "recipient": "person@review.test", "contact_source": "QA",
        "compliance_status": "PENDING_REVIEW", "status": "READY_FOR_REVIEW",
    })

    assert can_contact(db, outreach_id=outreach.id).decision == "BLOCKED_COMPLIANCE"
    outreach.compliance_status = "VALID"
    assert can_contact(db, outreach_id=outreach.id).decision == "BLOCKED_NO_HUMAN_APPROVAL"


def test_can_contact_approved_returns_human_initiated_eligible(db, legal_env):
    organization = org(db)
    manager = actor(db, organization)
    outreach = create_outreach(db, actor=manager, payload={
        "company_name": "Allowed Co", "recipient": "person@allowed.test", "contact_source": "QA", "compliance_status": "VALID",
    })
    approve_outreach_route(outreach.id, db=db, actor=manager)

    decision = can_contact(db, outreach_id=outreach.id)
    assert decision.eligible is True
    assert decision.decision == "ELIGIBLE_FOR_HUMAN_INITIATED_CONTACT"
    assert "AUTO_SEND" not in decision.as_dict().values()


def test_approval_role_authorization(db, legal_env):
    organization = org(db)
    broker = actor(db, organization, role="BROKER")
    manager = actor(db, organization, role="GERENTE")
    outreach = create_outreach(db, actor=manager, payload={
        "company_name": "Auth Co", "recipient": "person@auth.test", "contact_source": "QA", "compliance_status": "VALID",
    })

    with pytest.raises(HTTPException) as exc:
        approve_outreach_route(outreach.id, db=db, actor=broker)
    assert exc.value.status_code == 403


def test_admin_routes_authorize_and_scope_by_organization(db, legal_env):
    org_a = org(db, "a")
    org_b = org(db, "b")
    manager_a = actor(db, org_a, username="manager-a")
    manager_b = actor(db, org_b, username="manager-b")
    create_suppression(SuppressionIn(email="a@example.com"), db=db, actor=manager_a)
    create_suppression(SuppressionIn(email="b@example.com"), db=db, actor=manager_b)
    db.commit()

    scoped = list_suppressions(db=db, actor=manager_a)["items"]
    assert [item["email"] for item in scoped] == ["a@example.com"]


def test_root_can_create_global_domain_suppression(db):
    organization = org(db)
    root = actor(db, organization, role="ROOT")
    result = create_suppression(SuppressionIn(domain="blocked.test", scope="DOMAIN", organization_id=None), db=db, actor=root)
    db.commit()

    assert result["suppression"]["organization_id"] is None
    assert result["suppression"]["domain"] == "blocked.test"


def test_root_can_create_outreach_for_selected_organization(db, legal_env):
    organization = org(db, "canonical")
    root = actor(db, None, role="ROOT")

    created = create_outreach_route(
        OutreachIn(
            organization_id=organization.id,
            company_name="QA Outreach",
            recipient="qa-outreach@example.invalid",
            contact_source="QA",
            compliance_status="VALID",
        ),
        db=db,
        actor=root,
    )

    assert created["organization_id"] == organization.id


def test_frontend_outreach_selector_uses_authorized_organization_list():
    frontend = Path(__file__).parents[2].joinpath("frontend/index.html").read_text()

    assert 'id="commercialOutreachOrganization"' in frontend
    assert "${API_BASE}/organization/available" in frontend
    assert "organization_id: organizationId ? Number(organizationId) : null" in frontend
    assert "Seleccione una organización." in frontend
    assert "organization_id: 1" not in frontend


def test_root_outreach_requires_existing_selected_organization(db, legal_env):
    root = actor(db, None, role="ROOT")

    with pytest.raises(HTTPException) as missing:
        create_outreach_route(
            OutreachIn(company_name="QA Missing Org", recipient="qa@example.invalid", contact_source="QA"),
            db=db,
            actor=root,
        )
    assert missing.value.status_code == 400

    with pytest.raises(HTTPException) as nonexistent:
        create_outreach_route(
            OutreachIn(organization_id=999, company_name="QA Bad Org", recipient="qa@example.invalid", contact_source="QA"),
            db=db,
            actor=root,
        )
    assert nonexistent.value.status_code == 400


def test_org_scoped_outreach_cannot_cross_tenant(db, legal_env):
    organization_a = org(db, "a")
    organization_b = org(db, "b")
    manager = actor(db, organization_a, role="GERENTE")

    with pytest.raises(HTTPException) as cross_tenant:
        create_outreach_route(
            OutreachIn(
                organization_id=organization_b.id,
                company_name="QA Cross Tenant",
                recipient="qa-cross@example.invalid",
                contact_source="QA",
            ),
            db=db,
            actor=manager,
        )
    assert cross_tenant.value.status_code == 403

    own = create_outreach_route(
        OutreachIn(
            organization_id=organization_a.id,
            company_name="QA Own Tenant",
            recipient="qa-own@example.invalid",
            contact_source="QA",
        ),
        db=db,
        actor=manager,
    )
    assert own["organization_id"] == organization_a.id


def test_outreach_create_list_approve_and_audit(db, legal_env):
    organization = org(db)
    manager = actor(db, organization)
    created = create_outreach_route(OutreachIn(company_name="Ledger Co", recipient="lead@ledger.test", contact_source="QA", compliance_status="VALID"), db=db, actor=manager)
    approved = approve_outreach_route(created["id"], db=db, actor=manager)
    decision = can_contact_route(created["id"], db=db, actor=manager)

    assert list_outreach(db=db, actor=manager)["items"][0]["id"] == created["id"]
    assert approved["status"] == "APPROVED"
    assert decision["decision"] == "ELIGIBLE_FOR_HUMAN_INITIATED_CONTACT"
    assert db.query(CommercialAuditEvent).filter(CommercialAuditEvent.event_type.in_(["outreach_created", "outreach_approved"])).count() == 2


def test_anti_enumeration_honeypot_and_invalid_email_response(db):
    honeypot = _register_preference(db, request(), PreferenceIn(email="visible@example.com", action="STOP_MARKETING", website="bot"))
    invalid = _register_preference(db, request(), PreferenceIn(email="invalid", action="STOP_MARKETING"))

    assert honeypot == invalid == {"message": "Su preferencia ha sido registrada."}
    assert db.query(GlobalSuppression).count() == 0


def test_rate_limit_records_public_preference_attempts(db):
    _register_preference(db, request(), PreferenceIn(email="rate@example.com", action="STOP_MARKETING"))
    assert db.execute(text("SELECT COUNT(*) FROM auth_rate_limits")).scalar() >= 1


def test_privacy_version_is_central_constant(db, legal_env):
    organization = org(db)
    manager = actor(db, organization)
    row = create_outreach(db, actor=manager, payload={
        "company_name": "Version Co", "recipient": "person@version.test", "contact_source": "QA", "compliance_status": "VALID",
    })
    assert row.privacy_notice_version == PRIVACY_NOTICE_VERSION == "TS_PRIVACY_2026_01"
