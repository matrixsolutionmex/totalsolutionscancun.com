import asyncio
import os
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
from app.models.organization import Organization
from app.models.professional_application import ProfessionalApplication
from app.models.user import User
from app.routes.lead_document_routes import download_lead_document
from app.routes.professional_network_routes import application_file
from app.routes.user_routes import login as legacy_user_login, update_own_profile
from app.schemas.user_schema import LoginRequest, UserUpdate
from app.core.security import hash_password
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
