from datetime import datetime, timedelta
from pathlib import Path

import pytest
import app.main  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
