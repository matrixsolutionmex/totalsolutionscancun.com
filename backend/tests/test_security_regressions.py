import os
from pathlib import Path

import pytest
from fastapi import HTTPException
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
from app.models.user import User
from app.routes.lead_document_routes import download_lead_document
from app.routes.user_routes import login as legacy_user_login
from app.schemas.user_schema import LoginRequest
from app.core.security import hash_password


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


def make_request():
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "path": "/users/login",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver"), (b"user-agent", b"security-test")],
        }
    )


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

    with pytest.raises(HTTPException) as blocked:
        download_lead_document(lead.id, doc.id, db, other)
    assert blocked.value.status_code == 403


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
