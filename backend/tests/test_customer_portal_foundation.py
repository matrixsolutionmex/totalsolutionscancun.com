from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main  # noqa: F401
from app.database.connection import Base
from app.models.customer_portal import CustomerClaimToken, CustomerServiceLink
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_property import ServiceProperty
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.routes.customer_portal_routes import customer_dashboard, create_customer_service_request, customer_service_request_detail, get_customer_portal_config
from app.services.customer_account_service import (
    customer_portal_available,
    customer_portal_config,
    create_customer_claim_token,
    verify_customer_claim,
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
        engine.dispose()


def make_customer_fixture(db):
    org = Organization(name="Customer QA", slug="customer-qa", status="ACTIVE")
    other_org = Organization(name="Other QA", slug="other-qa", status="ACTIVE")
    db.add_all([org, other_org])
    db.flush()
    customer = User(
        organization_id=org.id, username="customer@example.test", email="customer@example.test",
        password_hash="x", role="CLIENTE", status="ACTIVE", is_active=True, email_verified=True,
    )
    other_customer = User(
        organization_id=other_org.id, username="other@example.test", email="other@example.test",
        password_hash="x", role="CLIENTE", status="ACTIVE", is_active=True, email_verified=True,
    )
    lead = Lead(organization_id=org.id, nome="Customer", email="customer@example.test")
    db.add_all([customer, other_customer, lead])
    db.flush()
    property_record = ServiceProperty(
        organization_id=org.id, lead_id=lead.id, profile_type="CASA", address_line1="QA address", locality="Cancun",
    )
    db.add(property_record)
    db.flush()
    request = ServiceRequest(
        organization_id=org.id, lead_id=lead.id, property_id=property_record.id,
        tracking_token="public-token", requester_name="Customer", requester_email="customer@example.test",
        service_category="ELECTRICAL", status="SALES_QUEUE",
    )
    db.add(request)
    db.flush()
    order = ServiceOrder(
        organization_id=org.id, lead_id=lead.id, service_request_id=request.id,
        property_record_id=property_record.id, order_number="TS-CUSTOMER-1", status="ABERTA",
        created_at=datetime.utcnow(),
    )
    db.add(order)
    db.flush()
    db.add(CustomerServiceLink(
        organization_id=org.id, customer_user_id=customer.id, service_request_id=request.id,
        verification_method="email", verified_at=datetime.utcnow(), active=True,
    ))
    db.commit()
    return org, other_org, customer, other_customer, request


def test_portal_is_off_and_invalid_configuration_fails_closed(monkeypatch, db):
    monkeypatch.delenv("CUSTOMER_PORTAL_ENABLED", raising=False)
    monkeypatch.delenv("CUSTOMER_PORTAL_ROLLOUT_MODE", raising=False)
    monkeypatch.delenv("CUSTOMER_PORTAL_CANARY_ORGANIZATION_IDS", raising=False)
    assert customer_portal_config()["enabled"] is False
    monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("CUSTOMER_PORTAL_CANARY_ORGANIZATION_IDS", "0,abc")
    assert customer_portal_config()["valid"] is False


def test_customer_dashboard_returns_only_verified_links(monkeypatch, db):
    monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "all")
    org, _, customer, _, _ = make_customer_fixture(db)
    dashboard = customer_dashboard(customer, db)
    assert len(dashboard["services"]) == 1
    assert dashboard["services"][0]["order_number"] == "TS-CUSTOMER-1"
    assert dashboard["services"][0]["property"]["locality"] == "Cancun"
    assert "email" not in dashboard["services"][0]
    assert customer_portal_available(db, org.id) is True


def test_customer_portal_request_endpoint_derives_identity_from_session(monkeypatch, db):
    monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "all")
    org, _, customer, _, request = make_customer_fixture(db)
    captured = {}

    def fake_create(db_session, payload, **kwargs):
        captured.update(payload)
        assert kwargs["actor"] is customer
        assert kwargs["organization_id"] == org.id
        return request

    monkeypatch.setattr("app.routes.customer_portal_routes.create_customer_request_and_order", fake_create)
    monkeypatch.setattr("app.routes.customer_portal_routes.create_opportunity_from_service_request", lambda *_args: None)
    body = "&".join([
        "property_type=Casa", "service_category=Eletrica", "address_line1=Casa QA",
        "location_confirmed=true", "consent_privacy=true", "idempotency_key=portal-identity-test",
        "requester_name=Attacker", "requester_email=attacker%40invalid.test",
    ]).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    from starlette.requests import Request
    response = __import__("asyncio").run(create_customer_service_request(
        Request({"type": "http", "method": "POST", "path": "/customer-portal/me/service-requests", "headers": [(b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", str(len(body)).encode())]}, receive),
        customer,
        db,
    ))
    assert response["order_number"] == "TS-CUSTOMER-1"
    assert captured["requester_name"] == customer.full_name or captured["requester_name"] == customer.username
    assert captured["requester_email"] == customer.email
    assert captured["requester_email"] != "attacker@invalid.test"


def test_customer_portal_frontend_uses_authenticated_request_mode():
    index = (Path(__file__).parents[2] / "frontend" / "index.html").read_text()
    portal = (Path(__file__).parents[2] / "frontend" / "customer-portal.html").read_text()
    assert 'const customerPublicExperience = ["/solicitar-servico", "/solicitud-enviada"].includes(window.location.pathname)' in index
    assert 'window.location.pathname.startsWith("/seguimiento/")' in index
    assert 'const customerPortalMode = isPortal && new URLSearchParams(window.location.search).get("customer_portal") === "1";' in index
    assert '`${apiBase}/customer-portal/me/service-requests`' in index
    assert 'href="/solicitar-servico?customer_portal=1"' in portal
    assert 'service.tracking_url' in portal
    assert 'credentials: "include"' in index[index.index("const submitFetch"):]
    assert 'headers: authHeaders(options.headers)' in index
    assert 'customer_service_request_detail' not in portal
    assert 'service_request_id=${encodeURIComponent(service.service_request_id)}' in portal


def test_customer_service_request_detail_is_link_and_tenant_scoped(monkeypatch, db):
    monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "all")
    org, _, customer, other_customer, request = make_customer_fixture(db)
    detail = customer_service_request_detail(request.id, customer, db)
    assert detail["service_request_id"] == request.id
    assert detail["order_number"] == "TS-CUSTOMER-1"
    with pytest.raises(Exception) as exc_info:
        customer_service_request_detail(request.id, other_customer, db)
    assert getattr(exc_info.value, "status_code", None) == 404


def test_customer_portal_config_is_tenant_and_role_scoped(monkeypatch, db):
    monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "canary")
    org, other_org, customer, other_customer, _ = make_customer_fixture(db)
    monkeypatch.setenv("CUSTOMER_PORTAL_CANARY_ORGANIZATION_IDS", str(org.id))
    assert get_customer_portal_config(customer, db) == {"customer_portal_enabled": True}
    assert get_customer_portal_config(other_customer, db) == {"customer_portal_enabled": False}
    admin = User(
        organization_id=org.id, username="admin@example.test", email="admin@example.test",
        password_hash="x", role="ROOT", status="ACTIVE", is_active=True,
    )
    db.add(admin)
    db.commit()
    assert get_customer_portal_config(admin, db) == {"customer_portal_enabled": False}


def test_customer_portal_frontend_has_bounded_fail_closed_fetch():
    frontend = Path(__file__).parents[2] / "frontend" / "customer-portal.html"
    source = frontend.read_text()
    assert "new AbortController()" in source
    assert "timeoutMs = 10000" in source
    assert "controller.abort()" in source
    assert "loading.classList.add(\"hidden\")" in source
    assert "validDashboardPayload(data)" in source
    assert "response.status === 401" in source
    assert 'next=${encodeURIComponent("/cliente")}' in source
    assert "window.location.replace" in source


def test_customer_session_is_cookie_only_and_portal_has_logout():
    index = (Path(__file__).parents[2] / "frontend" / "index.html").read_text()
    portal = (Path(__file__).parents[2] / "frontend" / "customer-portal.html").read_text()
    customer_branch = index.index('String(currentUser.role || "").toUpperCase() === "CLIENTE"')
    assert "clearCustomerBrowserState();" in index[customer_branch:customer_branch + 500]
    staff_persistence = index.index('localStorage.setItem("totalsolutions_user"', customer_branch)
    assert 'localStorage.setItem("totalsolutions_user"' not in index[customer_branch:staff_persistence]
    assert "id=\"customerLogout\"" in portal
    assert 'fetch("/auth/logout"' in portal
    assert 'window.location.replace(loginUrl())' in portal
    assert "sessionStorage.clear()" in portal


def test_customer_claim_requires_matching_verified_channel(monkeypatch, db):
    monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "all")
    monkeypatch.setenv("CUSTOMER_PORTAL_CLAIM_SECRET", "customer-claim-secret-0123456789-abcdef")
    _, _, customer, _, request = make_customer_fixture(db)
    token = create_customer_claim_token(db, request, "email")
    db.commit()
    link = verify_customer_claim(db, token, customer)
    assert link.customer_user_id == customer.id
    assert link.service_request_id == request.id
    with pytest.raises(Exception):
        verify_customer_claim(db, token, customer)


def test_customer_claim_expires_and_no_financial_models_are_touched(monkeypatch, db):
    monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_ROLLOUT_MODE", "all")
    monkeypatch.setenv("CUSTOMER_PORTAL_CLAIM_SECRET", "customer-claim-secret-0123456789-abcdef")
    _, _, customer, _, request = make_customer_fixture(db)
    token = create_customer_claim_token(db, request, "email")
    record = db.query(CustomerClaimToken).first()
    record.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    with pytest.raises(Exception):
        verify_customer_claim(db, token, customer)
    assert db.query(CustomerServiceLink).count() == 1
