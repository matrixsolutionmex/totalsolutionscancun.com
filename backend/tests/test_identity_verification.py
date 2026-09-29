from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
import app.main  # noqa: F401,E402 - registers the existing model graph for foreign keys
from app.models.identity_verification import IdentityVerification
from app.models.organization import Organization
from app.models.user import User
from app.services.identity_verification_service import (
    get_identity_verification_payload,
    safe_identity_status,
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


def make_user(db, *, organization_id=None, role="BROKER"):
    user = User(
        organization_id=organization_id,
        username=f"identity-{role.lower()}-{organization_id or 'none'}",
        email="identity@example.test",
        password_hash="test-hash",
        role=role,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
        telefone="+52 999 000 0000",
    )
    db.add(user)
    db.flush()
    return user


def test_invalid_status_is_safe_and_never_verified():
    assert safe_identity_status("provider-secret-status") == "NOT_STARTED"
    assert safe_identity_status("VERIFIED") == "VERIFIED"


def test_flag_off_hides_identity_ui_and_sensitive_fields(monkeypatch, db):
    monkeypatch.delenv("IDENTITY_VERIFICATION_UI_ENABLED", raising=False)
    user = make_user(db)
    db.add(IdentityVerification(user_id=user.id, organization_id=None, status="VERIFIED", provider="vendor", external_reference="secret-ref"))
    db.commit()

    payload = get_identity_verification_payload(db, user)

    assert payload["available"] is False
    assert payload["badge"] is False
    assert payload["status"] == "NOT_STARTED"
    assert not {"provider", "external_reference", "document", "curp", "selfie"}.intersection(payload)


def test_verified_badge_requires_flag_exact_status_and_same_tenant(monkeypatch, db):
    monkeypatch.setenv("IDENTITY_VERIFICATION_UI_ENABLED", "true")
    monkeypatch.setenv("IDENTITY_PROVIDER", "metamap")
    monkeypatch.setenv("IDENTITY_PROVIDER_MODE", "sandbox")
    monkeypatch.setenv("METAMAP_CLIENT_ID", "client-public-test")
    monkeypatch.setenv("METAMAP_FLOW_ID", "flow-test")
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "all")
    org = Organization(name="Identity Org", slug="identity-org")
    other_org = Organization(name="Other Org", slug="other-org")
    db.add_all([org, other_org])
    db.flush()
    user = make_user(db, organization_id=org.id, role="BROKER")
    db.add(IdentityVerification(user_id=user.id, organization_id=org.id, status="VERIFIED"))
    db.commit()

    payload = get_identity_verification_payload(db, user)
    assert payload["badge"] is True
    assert payload["status"] == "VERIFIED"

    user.organization_id = other_org.id
    db.commit()
    changed_tenant_payload = get_identity_verification_payload(db, user)
    assert changed_tenant_payload["badge"] is False
    assert changed_tenant_payload["status"] == "NOT_STARTED"


def test_role_does_not_grant_verified_badge(db, monkeypatch):
    monkeypatch.setenv("IDENTITY_VERIFICATION_UI_ENABLED", "true")
    monkeypatch.setenv("IDENTITY_PROVIDER", "metamap")
    monkeypatch.setenv("IDENTITY_PROVIDER_MODE", "sandbox")
    monkeypatch.setenv("METAMAP_CLIENT_ID", "client-public-test")
    monkeypatch.setenv("METAMAP_FLOW_ID", "flow-test")
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "all")
    user = make_user(db, role="ROOT")
    payload = get_identity_verification_payload(db, user)
    assert payload["badge"] is False
    assert payload["status"] == "NOT_STARTED"


def test_enabled_flag_without_migration_fails_closed(monkeypatch):
    monkeypatch.setenv("IDENTITY_VERIFICATION_UI_ENABLED", "true")
    monkeypatch.setenv("IDENTITY_PROVIDER", "metamap")
    monkeypatch.setenv("IDENTITY_PROVIDER_MODE", "sandbox")
    monkeypatch.setenv("METAMAP_CLIENT_ID", "client-public-test")
    monkeypatch.setenv("METAMAP_FLOW_ID", "flow-test")
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "all")

    class MissingMigrationDB:
        def query(self, _model):
            raise SQLAlchemyError("identity_verifications does not exist")

        def rollback(self):
            pass

    user = User(
        id=100,
        username="identity-missing-migration",
        password_hash="test-hash",
        role="BROKER",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    payload = get_identity_verification_payload(MissingMigrationDB(), user)
    assert payload["available"] is False
    assert payload["badge"] is False
    assert payload["status"] == "NOT_STARTED"


def test_disabled_flag_does_not_query_missing_migration(monkeypatch):
    monkeypatch.delenv("IDENTITY_VERIFICATION_UI_ENABLED", raising=False)

    class QueryMustNotRunDB:
        def query(self, _model):
            raise AssertionError("disabled identity UI must not query its table")

    user = User(id=101, username="identity-disabled", password_hash="test-hash", role="BROKER", status="ACTIVE", is_active=True, email_verified=True)
    payload = get_identity_verification_payload(QueryMustNotRunDB(), user)
    assert payload["available"] is False
    assert payload["badge"] is False


def test_frontend_uses_read_only_status_and_has_no_identity_upload():
    source = (Path(__file__).parents[2] / "frontend" / "index.html").read_text()
    assert "/identity-verification/me" in source
    assert "IDENTITY_VERIFICATION_UI_ENABLED" not in source or "available" in source
    assert "identityVerified" not in source or "identityStatusVerified" in source
    modal = source.split('id="identityVerificationInfoModal"', 1)[1].split("</div>", 1)[0]
    assert 'type="file"' not in modal


def test_frontend_identity_ui_fails_closed_when_unavailable():
    source = (Path(__file__).parents[2] / "frontend" / "index.html").read_text()
    assert 'id="identityVerificationActionButton" type="button" hidden disabled tabindex="-1"' in source
    assert "function resetIdentityVerificationUi()" in source
    assert "payload?.available !== true" in source
    assert "identityVerificationPayload?.available !== true" in source
    assert "resetIdentityVerificationUi();" in source
