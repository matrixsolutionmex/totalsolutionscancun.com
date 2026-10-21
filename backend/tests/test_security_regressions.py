import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import app.main  # noqa: F401
from app.database.connection import Base
from app.main import app
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.auth_security import AuthRateLimit
from app.models.organization import Organization
from app.models.professional_application import ProfessionalApplication
from app.models.user import User
from app.routes.lead_document_routes import download_lead_document
from app.routes.professional_network_routes import application_file
from app.routes.public_service_request_routes import _public_action_idempotency_key
from app.routes.user_routes import login as legacy_user_login, update_own_profile
from app.schemas.user_schema import LoginRequest, UserUpdate
from app.core.security import hash_password
from app.core import auth_security
from app.auth.jwt_handler import jwt_secret_key
from app.main import security_headers


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


def make_request(*, path="/users/login", headers=None, client_host="127.0.0.1"):
    request_headers = [(b"host", b"testserver"), (b"user-agent", b"security-test")]
    request_headers.extend(headers or [])
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": (client_host, 1234),
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": request_headers,
        }
    )


def test_tokenized_and_authenticated_responses_are_not_publicly_cacheable():
    async def call_next(request):
        return Response("synthetic")

    for path, headers in (
        ("/public/service-requests/synthetic-token", []),
        ("/public/service-requests/synthetic-token/tracking", []),
        ("/payments/public/service-requests/synthetic-token/checkout", []),
        ("/customer-portal/dashboard", [(b"cookie", b"ts_session=synthetic")]),
    ):
        response = asyncio.run(security_headers(make_request(path=path, headers=headers), call_next))
        assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
        assert response.headers["CDN-Cache-Control"] == "no-store"
        if "/service-requests/" in path:
            assert response.headers["Referrer-Policy"] == "no-referrer"

    tracking_page = asyncio.run(
        security_headers(make_request(path="/seguimiento/synthetic-token"), call_next)
    )
    assert tracking_page.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert tracking_page.headers["Referrer-Policy"] == "no-referrer"


def test_tracking_page_never_initializes_marketing_analytics():
    source = (Path(__file__).parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "marketingAnalytics?.shouldConfigureForPath(path) !== false && !isTracking" in source
    assert "\n      marketingAnalytics?.configure(window, document);" not in source


def test_public_action_idempotency_keys_do_not_persist_bearer_token(monkeypatch):
    monkeypatch.setenv("AUTH_AUDIT_PEPPER", "synthetic-public-tracking-pepper")
    token = "synthetic-bearer-token-never-store"

    first = _public_action_idempotency_key("accept", token)
    repeated = _public_action_idempotency_key("accept", token)
    other_action = _public_action_idempotency_key("problem", token)
    client_scoped = _public_action_idempotency_key("warranty", token, "client-retry-key")

    assert first == repeated
    assert first != other_action
    assert token not in first
    assert token not in client_scoped
    assert "client-retry-key" not in client_scoped


def test_production_without_explicit_jwt_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("ROOT_KEY", raising=False)
    monkeypatch.delenv("ROOT_PASSWORD", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY ausente"):
        jwt_secret_key()


def test_customer_cannot_mass_assign_account_or_plan_fields(db):
    customer = User(
        username="customer-profile-security",
        email="customer-profile-security@example.test",
        password_hash="x",
        full_name="Synthetic Customer",
        role="CLIENTE",
        plan="FREE",
        plan_max_brokers=1,
        plan_max_leads=100,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(customer)
    db.commit()

    payload = UserUpdate(
        full_name="Updated Customer",
        email="attacker@example.test",
        company="Elevated Company",
        plan="PRO",
        plan_max_brokers=999,
        plan_max_leads=9999,
    )

    with pytest.raises(HTTPException) as blocked:
        update_own_profile(customer.id, payload, customer, db)

    assert blocked.value.status_code == 403
    db.refresh(customer)
    assert customer.full_name == "Synthetic Customer"
    assert customer.email == "customer-profile-security@example.test"
    assert customer.company is None
    assert customer.plan == "FREE"
    assert customer.plan_max_brokers == 1
    assert customer.plan_max_leads == 100


def test_private_crm_uploads_are_not_publicly_downloadable():
    uploads_dir = Path(os.environ["UPLOADS_DIR"])
    private_file = uploads_dir / "lead_documents" / "9001" / "known.pdf"
    private_file.parent.mkdir(parents=True, exist_ok=True)
    private_file.write_bytes(b"synthetic private document")
    try:
        with TestClient(app) as client:
            response = client.get("/uploads/lead_documents/9001/known.pdf")
        assert response.status_code == 404
    finally:
        private_file.unlink(missing_ok=True)


def test_document_download_route_requires_authenticated_scope():
    with TestClient(app) as client:
        response = client.get("/leads/9001/documents/1/download")
    assert response.status_code in {401, 403, 404}


def test_scoped_document_download_allows_owner_and_blocks_other_organization(db, tmp_path, monkeypatch):
    import app.routes.lead_document_routes as document_routes

    monkeypatch.setattr(document_routes, "UPLOADS_DIR", tmp_path)
    org_a = Organization(name="Synthetic A", slug="synthetic-a")
    org_b = Organization(name="Synthetic B", slug="synthetic-b")
    owner = User(
        username="doc-owner",
        email="doc-owner@example.test",
        password_hash="x",
        full_name="Doc Owner",
        role="ROOT",
        organization_id=1,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    other = User(
        username="doc-other",
        email="doc-other@example.test",
        password_hash="x",
        full_name="Doc Other",
        role="ROOT",
        organization_id=2,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add_all([org_a, org_b])
    db.flush()
    owner.organization_id = org_a.id
    other.organization_id = org_b.id
    lead = Lead(nome="Synthetic customer", organization_id=org_a.id)
    db.add_all([owner, other, lead])
    db.flush()
    stored = tmp_path / "lead_documents" / str(lead.id) / "private.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"synthetic private bytes")
    doc = LeadDocument(
        organization_id=org_a.id,
        lead_id=lead.id,
        uploaded_by_user_id=owner.id,
        document_type="OTROS",
        file_name="private.pdf",
        file_path=f"/uploads/lead_documents/{lead.id}/private.pdf",
        file_mime="application/pdf",
        file_size=22,
    )
    db.add(doc)
    db.commit()

    response = download_lead_document(lead.id, doc.id, db, owner)
    assert response.path == stored
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["CDN-Cache-Control"] == "no-store"

    with pytest.raises(HTTPException) as blocked:
        download_lead_document(lead.id, doc.id, db, other)
    assert blocked.value.status_code == 403


def test_professional_application_download_is_private_and_no_store(db, tmp_path, monkeypatch):
    import app.routes.professional_network_routes as network_routes

    monkeypatch.setattr(network_routes, "UPLOADS_DIR", tmp_path)
    org = Organization(name="Professional Docs QA", slug="professional-docs-qa")
    actor = User(
        username="professional-doc-reviewer",
        email="professional-doc-reviewer@example.test",
        password_hash="x",
        full_name="Synthetic Reviewer",
        role="ROOT",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add_all([org, actor])
    db.flush()
    actor.organization_id = org.id
    application = ProfessionalApplication(
        public_code="TS-PRO-SECURITY",
        organization_id=org.id,
        full_name="Synthetic Applicant",
        email="synthetic-applicant@example.test",
        phone="+52 998 000 0003",
        whatsapp="+52 998 000 0003",
        city="Cancun",
        zone="Centro",
        professional_type="TECHNICIAN",
        experience="5",
        professional_level="INTERMEDIATE",
        specialties_json="[]",
        coverage_json="[]",
        availability="DAY",
        languages="es",
        uploads_json='[{"name":"evidence.pdf","path":"/uploads/professional-applications/TS-PRO-SECURITY/evidence.pdf"}]',
        consent_data=True,
        consent_contact=True,
        consent_profile=False,
    )
    db.add(application)
    db.commit()
    stored = tmp_path / "professional-applications" / application.public_code / "evidence.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"synthetic professional evidence")

    response = application_file(application.id, "evidence.pdf", db, actor)

    assert response.path == stored
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["CDN-Cache-Control"] == "no-store"


def test_legacy_login_is_rate_limited(db):
    user = User(
        username="legacy-security-user",
        email="legacy-security@example.test",
        password_hash=hash_password("password-segura"),
        full_name="Synthetic User",
        role="BROKER",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()

    payload = LoginRequest(username=user.username, password="password-segura")
    for _ in range(10):
        legacy_user_login(payload, make_request(), db)

    with pytest.raises(HTTPException) as blocked:
        legacy_user_login(payload, make_request(), db)
    assert blocked.value.status_code == 429


def test_legacy_login_limits_existing_and_missing_identifiers_with_generic_responses(db):
    user = User(
        username="legacy-identifier-user",
        email="legacy-identifier@example.test",
        password_hash=hash_password("password-segura"),
        full_name="Synthetic User",
        role="BROKER",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()

    details = []
    for username in (user.username, "missing-legacy-user"):
        with pytest.raises(HTTPException) as denied:
            legacy_user_login(
                LoginRequest(username=username, password="wrong-password"),
                make_request(client_host=f"192.0.2.{len(details) + 10}"),
                db,
            )
        assert denied.value.status_code == 401
        details.append(denied.value.detail)

    assert details == ["Usuario ou senha invalidos", "Usuario ou senha invalidos"]
    rows = db.query(AuthRateLimit).filter(AuthRateLimit.scope == "legacy-user-login-id").all()
    assert len(rows) == 2
    assert all(row.key_hash not in {user.username, "missing-legacy-user"} for row in rows)


def test_legacy_login_identifier_key_is_normalized(db):
    variants = (" Missing-Legacy-User ", "missing-legacy-user", "MISSING-LEGACY-USER")

    for index in range(8):
        with pytest.raises(HTTPException) as denied:
            legacy_user_login(
                LoginRequest(username=variants[index % len(variants)], password="wrong-password"),
                make_request(client_host=f"198.51.100.{index + 1}"),
                db,
            )
        assert denied.value.status_code == 401

    with pytest.raises(HTTPException) as limited:
        legacy_user_login(
            LoginRequest(username="  missing-LEGACY-user  ", password="wrong-password"),
            make_request(client_host="198.51.100.99"),
            db,
        )

    assert limited.value.status_code == 429
    rows = db.query(AuthRateLimit).filter(AuthRateLimit.scope == "legacy-user-login-id").all()
    assert len(rows) == 1
    assert rows[0].attempts == 9


def test_legacy_login_identifier_limit_expires_and_recovers(db):
    payload = LoginRequest(username="expiring-legacy-user", password="wrong-password")
    for index in range(8):
        with pytest.raises(HTTPException) as denied:
            legacy_user_login(payload, make_request(client_host=f"203.0.113.{index + 1}"), db)
        assert denied.value.status_code == 401

    row = db.query(AuthRateLimit).filter(AuthRateLimit.scope == "legacy-user-login-id").one()
    row.window_start = auth_security.now_utc() - timedelta(seconds=901)
    row.blocked_until = auth_security.now_utc() - timedelta(seconds=1)
    db.commit()

    with pytest.raises(HTTPException) as recovered:
        legacy_user_login(payload, make_request(client_host="203.0.113.99"), db)

    assert recovered.value.status_code == 401
    db.refresh(row)
    assert row.attempts == 1
    assert row.blocked_until is None


def test_valid_legacy_login_clears_identifier_limit_but_keeps_ip_limit(db):
    user = User(
        username="legacy-clear-user",
        email="legacy-clear@example.test",
        password_hash=hash_password("password-segura"),
        full_name="Synthetic User",
        role="BROKER",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    request = make_request(client_host="192.0.2.200")

    for _ in range(3):
        with pytest.raises(HTTPException) as denied:
            legacy_user_login(LoginRequest(username=user.username, password="wrong-password"), request, db)
        assert denied.value.status_code == 401

    authenticated = legacy_user_login(
        LoginRequest(username=user.username, password="password-segura"),
        request,
        db,
    )

    assert authenticated.id == user.id
    assert db.query(AuthRateLimit).filter(AuthRateLimit.scope == "legacy-user-login-id").count() == 0
    ip_row = db.query(AuthRateLimit).filter(AuthRateLimit.scope == "legacy-user-login-ip").one()
    assert ip_row.attempts == 4


def test_concurrent_legacy_login_attempts_share_one_identifier_bucket(tmp_path):
    database_path = tmp_path / "legacy-login-concurrency.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine)
    SessionForTest = sessionmaker(bind=engine)

    def attempt(index):
        session = SessionForTest()
        try:
            with pytest.raises(HTTPException) as denied:
                legacy_user_login(
                    LoginRequest(username="Concurrent-Legacy-User", password="wrong-password"),
                    make_request(client_host=f"198.18.0.{index + 1}"),
                    session,
                )
            return denied.value.status_code
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(attempt, range(8)))

        session = SessionForTest()
        try:
            row = session.query(AuthRateLimit).filter(AuthRateLimit.scope == "legacy-user-login-id").one()
            assert statuses == [401] * 8
            assert row.attempts == 8
        finally:
            session.close()
    finally:
        engine.dispose()
