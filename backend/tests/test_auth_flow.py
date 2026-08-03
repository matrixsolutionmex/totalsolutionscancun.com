from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response
from sqlalchemy.pool import StaticPool

from app.auth.jwt_handler import get_current_user, verify_access_token
from app.auth.routes import (
    google_login,
    login,
    request_password_recovery,
    register,
    request_reactivation,
    reset_password,
    start_mfa_setup_from_challenge,
    verify_email,
)
from app.core.auth_security import hotp, totp_counter, verify_mfa_challenge_token
from app.core.security import hash_password
from app.database.connection import Base
from app.models.auth_security import AuthAuditEvent, MfaRecoveryCode, PasswordResetToken, UserIdentity, UserSession
from app.models.lead import Lead
from app.models.notification import EmailOutbox, Notification, NotificationPreference, WebPushSubscription
from app.models.user import User
from app.models.user_lifecycle import UserLifecycleEvent, UserReactivationRequest
from app.routes.admin_routes import anonymize_user, approve_user, archive_user, reactivate_user, suspend_user
from app.schemas.auth_schema import (
    AuthLoginRequest,
    GoogleLoginRequest,
    MfaSetupChallengeStartRequest,
    PasswordRecoveryRequest,
    PasswordResetRequest,
    ReactivationRequestCreate,
    RegisterRequest,
    UserApprovalRequest,
)
from app.schemas.user_schema import UserAnonymizeRequest, UserArchiveRequest, UserLifecycleRequest, UserReactivateRequest


def create_test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            Lead.__table__,
            Notification.__table__,
            NotificationPreference.__table__,
            EmailOutbox.__table__,
            WebPushSubscription.__table__,
            UserLifecycleEvent.__table__,
            UserReactivationRequest.__table__,
            UserIdentity.__table__,
            UserSession.__table__,
            PasswordResetToken.__table__,
            MfaRecoveryCode.__table__,
            AuthAuditEvent.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def make_request(path="/auth/login"):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
        }
    )


def make_user(session, username, role="BROKER", *, manager_id=None, status="ACTIVE", is_active=True, email=None):
    user = User(
        username=username,
        email=email or f"{username}@totalsolutions.test",
        full_name=username.title(),
        password_hash=hash_password("user-password"),
        role=role,
        manager_id=manager_id,
        status=status,
        is_active=is_active,
        email_verified=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_public_registration_requires_verification_and_root_approval(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()

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
        make_request(),
        Response(),
        session,
    )
    claims = verify_access_token(authenticated.access_token)
    assert claims["sub"] == str(pending.id)
    assert authenticated.user.email == "broker@example.com"
    assert authenticated.user.role == "BROKER"

    session.close()


def test_suspension_blocks_login_revokes_token_and_disables_push(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    root = make_user(session, "root-lifecycle", "ROOT")
    technician = make_user(session, "tecnico-lifecycle", "BROKER")
    session.add(
        WebPushSubscription(
            user_id=technician.id,
            endpoint="https://push.example.test/device",
            p256dh="public",
            auth="auth",
            active=True,
        )
    )
    session.commit()

    authenticated = login(AuthLoginRequest(email=technician.email, password="user-password"), make_request(), Response(), session)
    old_token = authenticated.access_token

    suspended = suspend_user(
        technician.id,
        UserLifecycleRequest(reason="Saida temporaria da equipe"),
        session,
        root,
    )

    assert suspended.status == "SUSPENDED"
    assert suspended.is_active is False
    assert suspended.session_version == 1
    assert session.query(WebPushSubscription).filter_by(user_id=technician.id, active=True).count() == 0

    with pytest.raises(HTTPException) as login_blocked:
        login(AuthLoginRequest(email=technician.email, password="user-password"), make_request(), Response(), session)
    assert login_blocked.value.status_code == 403

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=old_token)
    with pytest.raises(HTTPException) as token_blocked:
        get_current_user(make_request(), credentials=credentials, db=session)
    assert token_blocked.value.status_code == 401
    assert session.query(UserLifecycleEvent).filter_by(user_id=technician.id, event_type="SUSPENDED").count() == 1

    session.close()


def test_reactivation_allows_new_login_and_records_audit(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    root = make_user(session, "root-reactivation", "ROOT")
    technician = make_user(session, "tecnico-reactivation", "BROKER")
    suspend_user(
        technician.id,
        UserLifecycleRequest(reason="Bloqueio preventivo"),
        session,
        root,
    )

    reactivated = reactivate_user(
        technician.id,
        UserReactivateRequest(reason="Retorno aprovado", role="BROKER", manager_id=None),
        session,
        root,
    )

    assert reactivated.status == "ACTIVE"
    assert reactivated.is_active is True
    assert reactivated.session_version == 2
    authenticated = login(AuthLoginRequest(email=technician.email, password="user-password"), make_request(), Response(), session)
    assert verify_access_token(authenticated.access_token)["session_version"] == 2
    assert session.query(UserLifecycleEvent).filter_by(user_id=technician.id, event_type="REACTIVATED").count() == 1

    session.close()


def test_suspended_email_creates_reactivation_request_without_duplicate(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    root = make_user(session, "root-requests", "ROOT")
    technician = make_user(session, "tecnico-requests", "BROKER", email="blocked@example.com")
    suspend_user(
        technician.id,
        UserLifecycleRequest(reason="Contrato pausado"),
        session,
        root,
    )
    user_count = session.query(User).count()

    with pytest.raises(HTTPException) as active_retry:
        request_reactivation(
            ReactivationRequestCreate(email=root.email, reason="Preciso acessar"),
            make_request("/auth/reactivation-request"),
            session,
        )
    assert active_retry.value.status_code == 409

    response = request_reactivation(
        ReactivationRequestCreate(email="blocked@example.com", reason="Retorno para nova escala"),
        make_request("/auth/reactivation-request"),
        session,
    )
    duplicate_response = request_reactivation(
        ReactivationRequestCreate(email="blocked@example.com", reason="Retorno para nova escala"),
        make_request("/auth/reactivation-request"),
        session,
    )

    assert response.request_id == duplicate_response.request_id
    assert session.query(User).count() == user_count
    assert session.query(UserReactivationRequest).filter_by(user_id=technician.id, status="PENDING").count() == 1
    assert session.query(Notification).filter_by(recipient_user_id=root.id, type="user_reactivation_requested").count() == 1

    session.close()


def test_anonymized_email_can_register_again_as_pending(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    root = make_user(session, "root-anonymize", "ROOT")
    technician = make_user(session, "tecnico-anonymize", "BROKER", email="removed@example.com")

    anonymize_user(
        technician.id,
        UserAnonymizeRequest(reason="Remocao LGPD solicitada", confirmation="ANONIMIZAR", client_action="UNASSIGN"),
        session,
        root,
    )
    session.refresh(technician)
    assert technician.email is None
    assert technician.username == f"removed-user-{technician.id}"
    assert technician.status == "ANONYMIZED"

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
            full_name="Novo Tecnico",
            email="removed@example.com",
            password="new-user-password",
        ),
        request,
        session,
    )

    assert "Cadastro recebido" in registration.message
    new_user = session.query(User).filter(User.email == "removed@example.com").one()
    assert new_user.id != technician.id
    assert new_user.status == "PENDING_EMAIL"
    assert session.query(UserLifecycleEvent).filter_by(user_id=technician.id, event_type="ANONYMIZED").count() == 1

    session.close()


def test_role_limits_last_root_and_archive_client_handling(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    root = make_user(session, "root-limits", "ROOT")
    manager_a = make_user(session, "manager-a", "GERENTE")
    manager_b = make_user(session, "manager-b", "GERENTE")
    technician_a = make_user(session, "technician-a", "BROKER", manager_id=manager_a.id)
    technician_b = make_user(session, "technician-b", "BROKER", manager_id=manager_b.id)
    session.add(
        Lead(
            nome="Cliente ativo",
            contato="9980000000",
            assigned_to_user_id=technician_a.id,
            pipeline="NOVO LEAD",
        )
    )
    session.commit()

    with pytest.raises(HTTPException) as technician_blocked:
        reactivate_user(
            technician_b.id,
            UserReactivateRequest(reason="Tentativa indevida", role="BROKER"),
            session,
            technician_a,
        )
    assert technician_blocked.value.status_code == 403

    with pytest.raises(HTTPException) as outside_team:
        suspend_user(
            technician_b.id,
            UserLifecycleRequest(reason="Fora da equipe"),
            session,
            manager_a,
        )
    assert outside_team.value.status_code == 404

    with pytest.raises(HTTPException) as own_root:
        anonymize_user(
            root.id,
            UserAnonymizeRequest(reason="Tentativa propria", confirmation="ANONIMIZAR", client_action="UNASSIGN"),
            session,
            root,
        )
    assert own_root.value.status_code == 400

    with pytest.raises(HTTPException) as client_conflict:
        archive_user(
            technician_a.id,
            UserArchiveRequest(reason="Desligado", client_action="KEEP"),
            session,
            root,
        )
    assert client_conflict.value.status_code == 409

    archived = archive_user(
        technician_a.id,
        UserArchiveRequest(reason="Desligado", client_action="UNASSIGN"),
        session,
        root,
    )
    assert archived.status == "ARCHIVED"
    assert session.query(Lead).filter(Lead.assigned_to_user_id == technician_a.id).count() == 0

    session.close()


def test_turnstile_required_and_rate_limit_are_enforced(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AUTH_SECURITY_TEST_MODE", "true")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
    session = create_test_session()
    make_user(session, "rate-user", email="rate@example.com")

    with pytest.raises(HTTPException) as missing_turnstile:
        login(AuthLoginRequest(email="rate@example.com", password="user-password"), make_request(), Response(), session)
    assert missing_turnstile.value.status_code == 403

    authenticated = login(
        AuthLoginRequest(email="rate@example.com", password="user-password", turnstile_token="test:login"),
        make_request(),
        Response(),
        session,
    )
    assert authenticated.access_token

    for _ in range(8):
        with pytest.raises(HTTPException):
            login(
                AuthLoginRequest(email="rate@example.com", password="wrong", turnstile_token="test:login"),
                make_request(),
                Response(),
                session,
            )
    with pytest.raises(HTTPException) as limited:
        login(
            AuthLoginRequest(email="rate@example.com", password="wrong", turnstile_token="test:login"),
            make_request(),
            Response(),
            session,
        )
    assert limited.value.status_code == 429
    assert session.query(AuthAuditEvent).filter(AuthAuditEvent.event_type.like("TURNSTILE%")).count() >= 1
    session.close()


def test_google_login_never_auto_links_existing_email(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AUTH_SECURITY_TEST_MODE", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-client-id")
    session = create_test_session()
    existing = make_user(session, "existing-google", email="same@example.com")

    google_payload = {
        "iss": "https://accounts.google.com",
        "aud": "google-client-id",
        "exp": 4102444800,
        "sub": "google-sub-1",
        "email": "same@example.com",
        "email_verified": True,
        "name": "Same User",
    }
    with pytest.raises(HTTPException) as blocked:
        google_login(GoogleLoginRequest(id_token=__import__("json").dumps(google_payload)), make_request("/auth/google"), Response(), session)
    assert blocked.value.status_code == 409
    assert session.query(UserIdentity).count() == 0

    google_payload["email"] = "new-google@example.com"
    google_payload["sub"] = "google-sub-2"
    response = google_login(GoogleLoginRequest(id_token=__import__("json").dumps(google_payload)), make_request("/auth/google"), Response(), session)
    assert response.access_token is None
    pending = session.query(User).filter(User.email == "new-google@example.com").one()
    assert pending.status == "PENDING_APPROVAL"
    assert pending.is_active is False
    assert session.query(UserIdentity).filter_by(user_id=pending.id, provider="google").count() == 1
    assert existing.status == "ACTIVE"
    session.close()


def test_root_requires_mfa_challenge_and_totp_setup(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AUTH_SECURITY_TEST_MODE", "true")
    session = create_test_session()
    root = make_user(session, "root-mfa", "ROOT", email="root-mfa@example.com")

    first_step = login(AuthLoginRequest(email=root.email, password="user-password"), make_request(), Response(), session)
    assert first_step.access_token is None
    assert first_step.mfa_required is True
    assert first_step.mfa_setup_required is True
    assert verify_mfa_challenge_token(first_step.mfa_challenge_token) == root.id

    setup = start_mfa_setup_from_challenge(
        MfaSetupChallengeStartRequest(challenge_token=first_step.mfa_challenge_token),
        session,
    )
    repeated_setup = start_mfa_setup_from_challenge(
        MfaSetupChallengeStartRequest(challenge_token=first_step.mfa_challenge_token),
        session,
    )
    assert repeated_setup.secret == setup.secret
    reset_setup = start_mfa_setup_from_challenge(
        MfaSetupChallengeStartRequest(challenge_token=first_step.mfa_challenge_token, reset_secret=True),
        session,
    )
    assert reset_setup.secret != setup.secret
    code = hotp(reset_setup.secret, totp_counter())
    from app.auth.routes import confirm_mfa_setup_from_challenge
    from app.schemas.auth_schema import MfaSetupChallengeConfirmRequest

    final = confirm_mfa_setup_from_challenge(
        MfaSetupChallengeConfirmRequest(challenge_token=first_step.mfa_challenge_token, code=code),
        make_request("/auth/mfa/setup/confirm-challenge"),
        Response(),
        session,
    )
    assert final.access_token
    session.refresh(root)
    assert root.mfa_enabled is True
    assert session.query(MfaRecoveryCode).filter_by(user_id=root.id, used_at=None).count() == 10
    session.close()
