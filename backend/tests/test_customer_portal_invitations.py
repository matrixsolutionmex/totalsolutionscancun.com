from datetime import datetime, timedelta
from pathlib import Path

import pytest
import app.main  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request
from starlette.responses import Response

from app.database.connection import Base
from app.models.customer_invitation import CustomerPortalInvitation, CustomerPortalInvitationEvent
from app.models.notification import EmailOutbox
from app.models.organization import Organization
from app.models.lead import Lead
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.customer_account_service import customer_invitation_config
from app.services.customer_invitation_service import (
    activate_new_customer,
    consume_existing_customer_invitation,
    create_invitation,
    inspect_token,
    resend_invitation,
    revoke_invitation,
)
from app.models.customer_portal import CustomerServiceLink, CustomerClaimToken
from app.services.customer_account_service import create_customer_claim_token
from app.auth.jwt_handler import create_access_token, customer_route_allowed, get_current_user
from app.auth.routes import issue_authenticated_response


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


def fixture(db):
    org = Organization(name="Portal QA", slug="portal-invites", status="ACTIVE")
    other = Organization(name="Other QA", slug="portal-invites-other", status="ACTIVE")
    db.add_all([org, other])
    db.flush()
    admin = User(organization_id=org.id, username="admin", email="admin@test.invalid", password_hash="x", role="GERENTE", status="ACTIVE", is_active=True)
    lead = Lead(organization_id=org.id, nome="QA", email="qa@example.test")
    db.add(admin)
    db.add(lead)
    db.flush()
    request = ServiceRequest(organization_id=org.id, lead_id=lead.id, tracking_token="portal-qa-token", requester_email="qa@example.test", requester_phone="+52 998 000 0001", requester_name="QA", service_category="ELECTRICAL", status="SALES_QUEUE")
    db.add(request)
    db.commit()
    return org, other, admin, request


def enable(monkeypatch, org_id):
    monkeypatch.setenv("CUSTOMER_PORTAL_INVITATIONS_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_INVITATIONS_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("CUSTOMER_PORTAL_INVITATIONS_CANARY_ORGANIZATION_IDS", str(org_id))
    monkeypatch.setenv("CUSTOMER_PORTAL_CLAIM_SECRET", "customer-claim-secret-0123456789-abcdef")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://totalsolutionscancun.com")


def test_invitation_is_feature_gated_and_fail_closed(monkeypatch):
    monkeypatch.delenv("CUSTOMER_PORTAL_INVITATIONS_ENABLED", raising=False)
    monkeypatch.delenv("CUSTOMER_PORTAL_INVITATIONS_ROLLOUT_MODE", raising=False)
    monkeypatch.delenv("CUSTOMER_PORTAL_INVITATIONS_CANARY_ORGANIZATION_IDS", raising=False)
    assert customer_invitation_config()["enabled"] is False
    monkeypatch.setenv("CUSTOMER_PORTAL_INVITATIONS_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_PORTAL_INVITATIONS_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("CUSTOMER_PORTAL_INVITATIONS_CANARY_ORGANIZATION_IDS", "0,abc")
    assert customer_invitation_config()["valid"] is False


def test_public_origin_accepts_only_canonical_https_and_encodes_token(monkeypatch):
    from app.services.customer_invitation_service import _activation_url, _validated_public_origin

    for value in ("https://totalsolutionscancun.com", "https://totalsolutionscancun.com/"):
        monkeypatch.setenv("PUBLIC_BASE_URL", value)
        assert _validated_public_origin() == "https://totalsolutionscancun.com"
    assert _activation_url("a/b?c", origin="https://totalsolutionscancun.com") == (
        "https://totalsolutionscancun.com/cliente/activar?token=a%2Fb%3Fc"
    )


@pytest.mark.parametrize("origin", [
    "http://totalsolutionscancun.com",
    "https://www.totalsolutionscancun.com",
    "https://evil.tld",
    "https://totalsolutionscancun.com.evil.tld",
    "https://evil.tld/totalsolutionscancun.com",
    "https://totalsolutionscancun.com:443",
    "https://user:pass@totalsolutionscancun.com",
    "https://totalsolutionscancun.com/path",
    "https://totalsolutionscancun.com?x=1",
    "https://totalsolutionscancun.com/#fragment",
    "//totalsolutionscancun.com",
    "http://127.0.0.1",
    "http://localhost",
    " https://totalsolutionscancun.com",
    "https://totalsolutionscancun.com ",
    "https://totalsolutionscancun.com\\path",
    "https://totalsolutionscancun.com\n",
    "https://TOTALSOLUTIONSCANCUN.COM",
    "https://xn--totalsolutionscancun-9w8c.com",
    "",
])
def test_invalid_public_origin_fails_closed_without_writes(monkeypatch, db, origin):
    org, _, admin, request = fixture(db)
    enable(monkeypatch, org.id)
    monkeypatch.setenv("PUBLIC_BASE_URL", origin)

    with pytest.raises(Exception) as error:
        create_invitation(
            db,
            actor=admin,
            service_request_id=request.id,
            channel="EMAIL",
            language="es",
            idempotency_key=f"invalid-origin-{abs(hash(origin))}",
        )

    assert getattr(error.value, "status_code", None) == 503
    assert db.query(CustomerPortalInvitation).count() == 0
    assert db.query(CustomerPortalInvitationEvent).count() == 0
    assert db.query(EmailOutbox).count() == 0
    assert db.query(CustomerServiceLink).count() == 0


def test_invalid_public_origin_cannot_partially_revoke_on_resend(monkeypatch, db):
    org, _, admin, request = fixture(db)
    enable(monkeypatch, org.id)
    invitation, _ = create_invitation(
        db,
        actor=admin,
        service_request_id=request.id,
        channel="EMAIL",
        language="es",
        idempotency_key="origin-resend-valid",
    )
    db.commit()

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://evil.tld")
    with pytest.raises(Exception) as error:
        resend_invitation(db, invitation, admin, "origin-resend-invalid")

    assert getattr(error.value, "status_code", None) == 503
    db.rollback()
    assert db.query(CustomerPortalInvitation).one().status == "QUEUED"
    assert db.query(CustomerPortalInvitationEvent).count() == 2
    assert db.query(EmailOutbox).count() == 1


def test_invitation_hashes_token_and_activation_is_atomic(monkeypatch, db):
    org, _, admin, request = fixture(db)
    enable(monkeypatch, org.id)
    invitation, raw = create_invitation(db, actor=admin, service_request_id=request.id, channel="EMAIL", language="pt-BR", idempotency_key="invite-001")
    assert raw and raw not in invitation.token_hash
    assert invitation.masked_destination == "qa***@example.test"
    assert inspect_token(db, raw).id == invitation.id
    user = activate_new_customer(db, raw, "Cliente QA", "uma-senha-segura", "qa@example.test", None)
    db.commit()
    assert user.role == "CLIENTE"
    assert db.query(CustomerPortalInvitation).one().status == "CONSUMED"
    assert db.query(CustomerPortalInvitationEvent).filter_by(event_type="CLAIM_CONSUMED").count() == 1
    assert inspect_token(db, raw) is None


def test_expired_revoked_and_reissued_tokens_fail_closed(monkeypatch, db):
    org, _, admin, request = fixture(db)
    enable(monkeypatch, org.id)
    invitation, raw = create_invitation(db, actor=admin, service_request_id=request.id, channel="EMAIL", language="es", idempotency_key="invite-002")
    invitation.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    assert inspect_token(db, raw) is None
    with pytest.raises(Exception):
        activate_new_customer(db, raw, "Cliente QA", "uma-senha-segura", "qa@example.test", None)
    invitation.expires_at = datetime.utcnow() + timedelta(minutes=30)
    invitation.status = "QUEUED"
    invitation.revoked_at = None
    db.commit()
    revoke_invitation(db, invitation, admin)
    db.commit()
    assert inspect_token(db, raw) is None
    replacement, replacement_raw = resend_invitation(db, invitation, admin, "invite-003")
    db.commit()
    assert replacement.id != invitation.id
    assert replacement_raw and inspect_token(db, replacement_raw).id == replacement.id
    assert inspect_token(db, raw) is None


def test_tenant_and_staff_conflicts_are_blocked(monkeypatch, db):
    org, other, admin, request = fixture(db)
    enable(monkeypatch, org.id)
    staff = User(organization_id=org.id, username="staff", email="qa@example.test", password_hash="x", role="BROKER", status="ACTIVE", is_active=True)
    db.add(staff)
    db.commit()
    with pytest.raises(Exception):
        create_invitation(db, actor=admin, service_request_id=request.id, channel="EMAIL", language="es", idempotency_key="invite-004")
    other_admin = User(organization_id=other.id, username="other-admin", email="other@test.invalid", password_hash="x", role="GERENTE", status="ACTIVE", is_active=True)
    db.add(other_admin)
    db.commit()
    with pytest.raises(Exception):
        create_invitation(db, actor=other_admin, service_request_id=request.id, channel="EMAIL", language="es", idempotency_key="invite-005")


def test_activation_page_is_localized_and_requires_normal_login_after_claim():
    activation = (Path(__file__).parents[2] / "frontend" / "customer-portal-activate.html").read_text()
    index = (Path(__file__).parents[2] / "frontend" / "index.html").read_text()
    assert "history.replaceState" in activation
    assert "Cuenta activada. Entra para abrir tu panel." in activation
    assert "Account activated. Sign in to open your dashboard." in activation
    assert "Conta ativada. Entre para abrir seu painel." in activation
    assert "loginLink" in activation
    assert "totalsolutions_access_token" not in activation
    assert "loginDestination" in index
    assert "window.location.replace(loginDestination)" in index
    assert "!candidate.includes(\"://\")" in index


@pytest.mark.parametrize("path", [
    "/auth/me",
    "/auth/logout",
    "/auth/google/link",
    "/auth/google/link/status",
    "/users/me/heartbeat",
    "/users/42/profile",
    "/customer-portal/config",
    "/customer-portal/me/dashboard",
])
def test_customer_auth_allowlist_keeps_only_account_and_portal_routes(path):
    assert customer_route_allowed(path) is True


@pytest.mark.parametrize("path", [
    "/",
    "/leads",
    "/board",
    "/users",
    "/users/42",
    "/users/42/profile-photo",
    "/network/organizations",
    "/payments",
    "/ledger",
    "/technician-earnings/me",
    "/identity-verification/review",
    "/admin/config",
])
def test_customer_auth_allowlist_blocks_staff_and_financial_routes(path):
    assert customer_route_allowed(path) is False


def test_customer_dependency_blocks_staff_routes_after_valid_authentication(db):
    org = Organization(name="Isolation QA", slug="isolation-qa", status="ACTIVE")
    db.add(org)
    db.flush()
    customer = User(
        organization_id=org.id,
        username="isolated-customer",
        email="isolated-customer@example.test",
        password_hash="x",
        role="CLIENTE",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(customer)
    db.commit()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=create_access_token(customer))

    def request_for(path):
        return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})

    assert get_current_user(request_for("/auth/me"), credentials=credentials, db=db).id == customer.id
    with pytest.raises(Exception) as error:
        get_current_user(request_for("/leads"), credentials=credentials, db=db)
    assert getattr(error.value, "status_code", None) == 403


def test_customer_authentication_omits_bearer_token_and_sets_session_cookie(db):
    org = Organization(name="Cookie Portal QA", slug="cookie-portal-qa", status="ACTIVE")
    db.add(org)
    db.flush()
    customer = User(
        organization_id=org.id,
        username="cookie-customer",
        email="cookie-customer@example.test",
        password_hash="x",
        role="CLIENTE",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(customer)
    db.flush()
    request = Request({"type": "http", "method": "POST", "path": "/auth/login", "headers": [], "client": ("127.0.0.1", 8000)})
    response = Response()
    result = issue_authenticated_response(db, request, response, customer, event_type="PASSWORD_LOGIN")
    assert result.access_token is None
    cookie = response.headers.get("set-cookie", "")
    assert "ts_session=" in cookie
    assert "HttpOnly" in cookie


def test_customer_login_redirect_precedes_crm_bootstrap():
    index = (Path(__file__).parents[2] / "frontend" / "index.html").read_text()
    redirect = "window.location.replace(`/cliente?lang=${encodeURIComponent(customerLanguage)}`);"
    assert redirect in index
    assert index.index(redirect) < index.index("applyUserMode();")
    assert "let customerRedirecting = false;" in index


def test_existing_customer_consumes_only_invitation_094_without_duplication(monkeypatch, db):
    org, _, admin, request = fixture(db)
    enable(monkeypatch, org.id)
    customer = User(
        organization_id=org.id,
        username="existing-customer",
        email="qa@example.test",
        password_hash="x",
        role="CLIENTE",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(customer)
    db.commit()

    invitation, raw = create_invitation(
        db,
        actor=admin,
        service_request_id=request.id,
        channel="EMAIL",
        language="es",
        idempotency_key="invite-existing-094",
    )
    link = consume_existing_customer_invitation(db, raw, customer)
    db.commit()
    assert link.customer_user_id == customer.id
    assert db.query(CustomerServiceLink).filter_by(customer_user_id=customer.id, service_request_id=request.id).count() == 1
    assert invitation.status == "CONSUMED"
    assert db.query(CustomerPortalInvitationEvent).filter_by(
        invitation_id=invitation.id,
        event_type="CLAIM_CONSUMED",
    ).count() == 1
    with pytest.raises(Exception):
        consume_existing_customer_invitation(db, raw, customer)
    assert db.query(CustomerServiceLink).filter_by(customer_user_id=customer.id, service_request_id=request.id).count() == 1


def test_invitation_094_never_falls_back_to_legacy_claim_token(monkeypatch, db):
    org, _, admin, request = fixture(db)
    enable(monkeypatch, org.id)
    customer = User(
        organization_id=org.id,
        username="legacy-customer",
        email="qa@example.test",
        password_hash="x",
        role="CLIENTE",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(customer)
    db.flush()
    legacy_raw = create_customer_claim_token(db, request, "email")
    db.commit()
    with pytest.raises(Exception):
        consume_existing_customer_invitation(db, legacy_raw, customer)
    assert db.query(CustomerServiceLink).count() == 0


def test_frontend_invitation_uses_accessible_modal_and_canonical_payload():
    frontend = Path(__file__).parents[2] / "frontend" / "index.html"
    index = frontend.read_text(encoding="utf-8")
    start = index.index("function submitCustomerInvitation")
    end = index.index("function openTrackingDiagnostic", start)
    invitation_flow = index[start:end]

    assert 'id="customerInvitationModal"' in index
    assert 'role="dialog"' in index
    assert 'aria-modal="true"' in index
    assert 'aria-labelledby="customerInvitationTitle"' in index
    assert 'aria-describedby="customerInvitationDescription"' in index
    assert "window.prompt" not in invitation_flow
    assert "window.confirm" not in invitation_flow
    assert "window.alert" not in invitation_flow
    assert "service_request_id: state.requestId" in invitation_flow
    assert "portal-invite:${state.requestId}:${state.channel}" in invitation_flow
    assert "language: currentLanguage || \"es\"" in invitation_flow
    assert "new AbortController()" in invitation_flow
    assert "CUSTOMER_INVITATION_TIMEOUT_MS" in invitation_flow
    assert 'name="customerInvitationChannel"' in index
    for key in (
        "portalInviteTitle",
        "portalInviteValidity",
        "portalInviteOneUse",
        "portalInviteCancel",
        "portalInviteSend",
        "portalInviteSending",
        "portalInviteError",
    ):
        assert index.count(f"{key}:") >= 3
