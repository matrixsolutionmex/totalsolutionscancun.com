from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from sqlalchemy.pool import StaticPool

from app.auth.jwt_handler import verify_access_token
from app.auth.routes import login, register, verify_email
from app.core.security import hash_password
from app.database.connection import Base
from app.models.user import User
from app.routes.admin_routes import approve_user
from app.schemas.auth_schema import AuthLoginRequest, RegisterRequest, UserApprovalRequest


def test_public_registration_requires_verification_and_root_approval(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    session = sessionmaker(bind=engine)()

    root = User(
        username="root",
        email="root@totalsolutions.test",
        full_name="Root",
        password_hash=hash_password("root-password"),
        role="ROOT",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    session.add(root)
    session.commit()
    session.refresh(root)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/auth/register",
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
    )
    registration = register(
        RegisterRequest(
            full_name="Broker Teste",
            email="broker@example.com",
            password="broker-password",
            company="Total Solutions Test",
        ),
        request,
        session,
    )
    token = parse_qs(urlparse(registration.verification_url).query)["token"][0]
    pending = session.query(User).filter(User.email == "broker@example.com").one()
    assert pending.status == "PENDING_EMAIL"
    assert pending.is_active is False

    verify_email(token, session)
    session.refresh(pending)
    assert pending.email_verified is True
    assert pending.status == "PENDING_APPROVAL"

    approved = approve_user(
        pending.id,
        UserApprovalRequest(role="BROKER", plan="STARTER", plan_max_leads=100),
        session,
        root,
    )
    assert approved.status == "ACTIVE"
    assert approved.is_active is True

    authenticated = login(
        AuthLoginRequest(email="broker@example.com", password="broker-password"),
        session,
    )
    claims = verify_access_token(authenticated.access_token)
    assert claims["sub"] == str(pending.id)
    assert authenticated.user.email == "broker@example.com"
    assert authenticated.user.role == "BROKER"

    session.close()
