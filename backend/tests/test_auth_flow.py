from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response
from sqlalchemy.pool import StaticPool

from app.auth import routes as auth_routes
from app.auth.jwt_handler import get_current_user, verify_access_token
from app.auth.routes import (
    confirm_mfa_setup_from_challenge,
    google_login,
    login,
    request_password_recovery,
    change_verification_email,
    register,
    request_reactivation,
    resend_verification,
    reset_password,
    start_mfa_setup_from_challenge,
    verify_email,
    verify_mfa,
)
from app.core import auth_security
from app.core.auth_security import hash_value, hotp, totp_counter, verify_mfa_challenge_token
from app.core.security import hash_password
from app.database.connection import Base
from app.models.auth_security import AuthAuditEvent, MfaRecoveryCode, PasswordResetToken, UserIdentity, UserSession
from app.models.lead import Lead
from app.models.notification import EmailOutbox, Notification, NotificationPreference, WebPushSubscription
from app.models.organization import Organization
from app.models.user import User
from app.models.user_lifecycle import UserLifecycleEvent, UserReactivationRequest
from app.routes.admin_routes import anonymize_user, approve_user, archive_user, reactivate_user, suspend_user
from app.schemas.auth_schema import (
    AuthLoginRequest,
    EmailVerificationChangeRequest,
    GoogleLoginRequest,
    MfaSetupChallengeStartRequest,
    MfaSetupChallengeConfirmRequest,
    MfaVerifyRequest,
    PasswordRecoveryRequest,
    PasswordResetRequest,
    ReactivationRequestCreate,
    EmailVerificationResendRequest,
    RegisterRequest,
    UserApprovalRequest,
)
from app.main import migrate_legacy_email_verification
from app.schemas.user_schema import UserAnonymizeRequest, UserArchiveRequest, UserLifecycleRequest, UserReactivateRequest


@pytest.fixture(autouse=True)
def clear_auth_rate_limits():
    auth_security._rate_buckets.clear()
    yield
    auth_security._rate_buckets.clear()


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
            Organization.__table__,
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


class CapturingSMTP:
    sent_messages = []
    fail_send = False

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def starttls(self):
        return None

    def login(self, *_args, **_kwargs):
        return None

    def send_message(self, message):
        if self.fail_send:
            raise RuntimeError("smtp-down")
        self.sent_messages.append(message)


def configure_smtp_capture(monkeypatch, *, fail=False):
    CapturingSMTP.sent_messages = []
    CapturingSMTP.fail_send = fail
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://totalsolutionscancun.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.setenv("SMTP_FROM", "no-reply@totalsolutionscancun.com")
    monkeypatch.setattr(auth_routes.smtplib, "SMTP", CapturingSMTP)


def token_from_last_verification_email():
    assert CapturingSMTP.sent_messages
    body = CapturingSMTP.sent_messages[-1].get_content()
    link = next(line.strip() for line in body.splitlines() if "/auth/verify-email?token=" in line)
    parsed = urlparse(link)
    return parse_qs(parsed.query)["token"][0]


def make_organization(session, slug="total-solutions-test", name="Total Solutions Test"):
    organization = Organization(name=name, slug=slug)
    session.add(organization)
    session.commit()
    session.refresh(organization)
    return organization


def make_user(
    session,
    username,
    role="BROKER",
    *,
    manager_id=None,
    status="ACTIVE",
    is_active=True,
    email=None,
    organization_id=None,
):
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
        organization_id=organization_id,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_public_registration_requires_verification_and_root_approval(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    configure_smtp_capture(monkeypatch)
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
    assert "token" not in registration.model_dump_json()
    assert "verify-email" not in registration.model_dump_json()
    assert registration.masked_email == "br****@example.com"
    token = token_from_last_verification_email()
    pending = session.query(User).filter(User.email == "broker@example.com").one()
    assert pending.status == "PENDING_EMAIL"
    assert pending.is_active is False
    assert pending.email_verification_token is None
    assert pending.email_verification_token_hash == hash_value(token)
    assert pending.email_verification_token_hash != token
    assert pending.email_verification_expires_at is not None

    with pytest.raises(HTTPException) as blocked_login:
        login(AuthLoginRequest(email="broker@example.com", password="broker-password"), make_request(), Response(), session)
    assert blocked_login.value.status_code == 403

    response = verify_email(token, session)
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["location"] == "/?email_confirmed=1"
    session.refresh(pending)
    assert pending.email_verified is True
    assert pending.status == "PENDING_ADMIN"
    assert pending.email_verification_token_hash is None

    reused = verify_email(token, session)
    assert reused.status_code == 400

    with pytest.raises(HTTPException) as blocked_pending_approval:
        login(AuthLoginRequest(email="broker@example.com", password="broker-password"), make_request(), Response(), session)
    assert blocked_pending_approval.value.status_code == 403

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


def test_email_verification_resend_invalidates_old_token_and_expiration(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    configure_smtp_capture(monkeypatch)
    session = create_test_session()
    registration = register(
        RegisterRequest(
            full_name="Expira Teste",
            email="expira@example.com",
            password="broker-password",
        ),
        make_request("/auth/register"),
        session,
    )
    assert "token" not in registration.model_dump_json()
    first_token = token_from_last_verification_email()
    pending = session.query(User).filter(User.email == "expira@example.com").one()
    pending.email_verification_expires_at = auth_routes.now_utc() - timedelta(seconds=1)
    pending.email_verification_sent_at = auth_routes.now_utc() - timedelta(seconds=90)
    session.commit()

    expired = verify_email(first_token, session)
    assert expired.status_code == 400

    resend = resend_verification(
        EmailVerificationResendRequest(email="expira@example.com"),
        make_request("/auth/resend-verification"),
        session,
    )
    assert resend.masked_email == "ex****@example.com"
    second_token = token_from_last_verification_email()
    assert second_token != first_token
    assert verify_email(first_token, session).status_code == 400
    assert verify_email(second_token, session).status_code == 303
    session.refresh(pending)
    assert pending.email_verified is True
    assert pending.status == "PENDING_ADMIN"

    session.close()


def test_raw_legacy_verification_token_is_not_accepted(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    user = make_user(session, "legacy-token-user", email="legacy-token@example.com", status="PENDING_EMAIL", is_active=False)
    user.email_verified = False
    user.email_verification_token = "raw-token-from-old-flow"
    user.email_verification_token_hash = None
    user.email_verification_expires_at = auth_routes.now_utc() + timedelta(minutes=10)
    session.commit()

    response = verify_email("raw-token-from-old-flow", session)

    assert response.status_code == 400
    session.refresh(user)
    assert user.email_verified is False
    assert user.status == "PENDING_EMAIL"
    session.close()


def test_change_verification_email_invalidates_old_token_and_prevents_takeover(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    configure_smtp_capture(monkeypatch)
    session = create_test_session()
    register(
        RegisterRequest(
            full_name="Change Email",
            email="wrong@example.com",
            password="broker-password",
        ),
        make_request("/auth/register"),
        session,
    )
    first_token = token_from_last_verification_email()
    make_user(session, "existing-email-owner", email="existing@example.com")

    duplicate_response = change_verification_email(
        EmailVerificationChangeRequest(old_email="wrong@example.com", new_email="existing@example.com"),
        make_request("/auth/change-verification-email"),
        session,
    )
    pending = session.query(User).filter(User.email == "wrong@example.com").one()
    assert duplicate_response.masked_email == "ex******@example.com"
    assert pending.email == "wrong@example.com"
    assert verify_email(first_token, session).status_code == 303

    register(
        RegisterRequest(
            full_name="Change Email Dois",
            email="typo@example.com",
            password="broker-password",
        ),
        make_request("/auth/register"),
        session,
    )
    old_token = token_from_last_verification_email()
    change_response = change_verification_email(
        EmailVerificationChangeRequest(old_email="typo@example.com", new_email="correct@example.com"),
        make_request("/auth/change-verification-email"),
        session,
    )
    assert change_response.masked_email == "co*****@example.com"
    changed = session.query(User).filter(User.email == "correct@example.com").one()
    assert changed.username == "correct@example.com"
    assert changed.email_verified is False
    new_token = token_from_last_verification_email()
    assert old_token != new_token
    assert verify_email(old_token, session).status_code == 400
    assert verify_email(new_token, session).status_code == 303
    session.refresh(changed)
    assert changed.status == "PENDING_ADMIN"
    session.close()


def test_smtp_configuration_requires_authenticated_secure_transport(monkeypatch, caplog):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    CapturingSMTP.sent_messages = []
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://totalsolutionscancun.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "no-reply@totalsolutionscancun.com")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    session = create_test_session()

    with caplog.at_level("WARNING"):
        register(
            RegisterRequest(
                full_name="Missing SMTP",
                email="missing-smtp@example.com",
                password="broker-password",
            ),
            make_request("/auth/register"),
            session,
        )

    pending = session.query(User).filter(User.email == "missing-smtp@example.com").one()
    assert pending.email_verified is False
    assert pending.status == "PENDING_EMAIL"
    assert CapturingSMTP.sent_messages == []
    assert "SMTP_USERNAME" in caplog.text
    assert "SMTP_PASSWORD" in caplog.text
    assert "/auth/verify-email?token=" not in caplog.text
    session.close()


def test_legacy_email_migration_only_verifies_active_approved_users():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session = sessionmaker(bind=engine)()
    session.execute(
        text(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email_verified BOOLEAN,
                is_active BOOLEAN,
                status VARCHAR,
                email_verification_token VARCHAR
            )
            """
        )
    )
    session.execute(
        text(
            """
            INSERT INTO users (id, email_verified, is_active, status, email_verification_token)
            VALUES
              (1, NULL, TRUE, 'ACTIVE', 'raw-active'),
              (2, NULL, FALSE, 'PENDING_EMAIL', 'raw-pending'),
              (3, NULL, FALSE, 'SUSPENDED', 'raw-suspended'),
              (4, NULL, FALSE, 'INACTIVE', 'raw-inactive')
            """
        )
    )
    migrate_legacy_email_verification(session)
    migrate_legacy_email_verification(session)
    rows = {
        row.id: row.email_verified
        for row in session.execute(text("SELECT id, email_verified FROM users ORDER BY id")).fetchall()
    }
    tokens = [row.email_verification_token for row in session.execute(text("SELECT email_verification_token FROM users")).fetchall()]
    assert rows == {1: 1, 2: 0, 3: 0, 4: 0}
    assert tokens == [None, None, None, None]
    session.close()


def test_smtp_failure_never_confirms_account_or_logs_token(monkeypatch, caplog):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    configure_smtp_capture(monkeypatch, fail=True)
    session = create_test_session()
    with caplog.at_level("WARNING"):
        registration = register(
            RegisterRequest(
                full_name="SMTP Falha",
                email="smtp-falha@example.com",
                password="broker-password",
            ),
            make_request("/auth/register"),
            session,
        )
    assert "token" not in registration.model_dump_json()
    assert "verify-email" not in registration.model_dump_json()
    assert CapturingSMTP.sent_messages == []
    pending = session.query(User).filter(User.email == "smtp-falha@example.com").one()
    assert pending.email_verified is False
    assert pending.status == "PENDING_EMAIL"
    assert pending.email_verification_token is None
    assert pending.email_verification_token_hash is not None
    assert "/auth/verify-email?token=" not in caplog.text
    assert pending.email_verification_token_hash not in caplog.text
    session.close()


def test_invalid_turnstile_blocks_registration_in_backend(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AUTH_SECURITY_TEST_MODE", "true")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
    session = create_test_session()
    with pytest.raises(HTTPException) as blocked:
        register(
            RegisterRequest(
                full_name="Turnstile Teste",
                email="turnstile@example.com",
                password="broker-password",
                turnstile_token="invalid",
            ),
            make_request("/auth/register"),
            session,
        )
    assert blocked.value.status_code == 403
    assert session.query(User).filter(User.email == "turnstile@example.com").count() == 0
    session.close()


def test_admin_cannot_approve_user_with_unconfirmed_email(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    root = make_user(session, "root-unconfirmed-approve", "ROOT")
    pending = make_user(session, "pending-unconfirmed", "BROKER", status="PENDING_EMAIL", is_active=False)
    pending.email_verified = False
    session.commit()

    with pytest.raises(HTTPException) as blocked:
        approve_user(
            pending.id,
            UserApprovalRequest(role="BROKER", plan="STARTER", plan_max_leads=100),
            session,
            root,
        )
    assert blocked.value.status_code == 400
    assert blocked.value.detail == "Correo pendiente de confirmación"
    session.close()


def test_registration_frontend_does_not_embed_verification_token_or_url():
    html = Path(__file__).parents[2].joinpath("frontend", "index.html").read_text(encoding="utf-8")
    assert "data.verification_url" not in html
    assert "verification_url" not in html
    assert "/auth/verify-email?token=" not in html


def test_user_activation_notifies_other_root_in_same_organization(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("WEB_PUSH_VAPID_PUBLIC_KEY", raising=False)
    session = create_test_session()
    org_a = make_organization(session, "org-admin-events-a", "Empresa A")
    org_b = make_organization(session, "org-admin-events-b", "Empresa B")
    actor_root = make_user(session, "root-actor-events", "ROOT", organization_id=org_a.id)
    recipient_root = make_user(session, "root-recipient-events", "ROOT", organization_id=org_a.id)
    outside_root = make_user(session, "root-outside-events", "ROOT", organization_id=org_b.id)
    pending = make_user(
        session,
        "supervisor-pending-events",
        "BROKER",
        status="PENDING_APPROVAL",
        is_active=False,
        organization_id=org_a.id,
    )

    approved = approve_user(
        pending.id,
        UserApprovalRequest(role="GERENTE", plan="STARTER", plan_max_leads=100),
        session,
        actor_root,
    )

    assert approved.status == "ACTIVE"
    recipient_notifications = session.query(Notification).filter_by(recipient_user_id=recipient_root.id).all()
    assert len(recipient_notifications) == 1
    assert recipient_notifications[0].type == "user_activated"
    assert recipient_notifications[0].title == "Nuevo usuario activado"
    assert "se incorporó como Supervisor" in recipient_notifications[0].message
    assert recipient_notifications[0].organization_id == org_a.id
    assert session.query(Notification).filter_by(recipient_user_id=actor_root.id).count() == 0
    assert session.query(Notification).filter_by(recipient_user_id=outside_root.id).count() == 0
    assert session.query(Notification).filter(Notification.type == "technician_activated").count() == 0

    session.close()


def test_technician_activation_uses_specific_event_without_duplicate(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("WEB_PUSH_VAPID_PUBLIC_KEY", raising=False)
    session = create_test_session()
    org = make_organization(session, "org-tech-events", "Empresa Tecnica")
    actor_root = make_user(session, "root-tech-actor", "ROOT", organization_id=org.id)
    recipient_root = make_user(session, "root-tech-recipient", "ROOT", organization_id=org.id)
    pending = make_user(
        session,
        "tecnico-pending-events",
        "BROKER",
        status="PENDING_APPROVAL",
        is_active=False,
        organization_id=org.id,
    )

    approve_user(
        pending.id,
        UserApprovalRequest(role="BROKER", plan="STARTER", plan_max_leads=100),
        session,
        actor_root,
    )

    notifications = session.query(Notification).filter_by(recipient_user_id=recipient_root.id).all()
    assert len(notifications) == 1
    assert notifications[0].type == "technician_activated"
    assert notifications[0].title == "Nuevo técnico registrado"
    assert "fue activado en el equipo técnico" in notifications[0].message
    assert session.query(Notification).filter(Notification.type == "user_activated").count() == 0

    session.close()


def test_single_root_receives_own_technician_activation_alert(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("WEB_PUSH_VAPID_PUBLIC_KEY", raising=False)
    session = create_test_session()
    org = make_organization(session, "org-single-root-tech-events", "Empresa Root Unico")
    actor_root = make_user(session, "root-single-tech-actor", "ROOT", organization_id=org.id)
    pending = make_user(
        session,
        "tecnico-single-root-events",
        "BROKER",
        status="PENDING_APPROVAL",
        is_active=False,
        organization_id=org.id,
    )

    approve_user(
        pending.id,
        UserApprovalRequest(role="BROKER", plan="STARTER", plan_max_leads=100),
        session,
        actor_root,
    )

    notifications = session.query(Notification).filter_by(recipient_user_id=actor_root.id).all()
    assert len(notifications) == 1
    assert notifications[0].type == "technician_activated"
    assert notifications[0].title == "Nuevo técnico registrado"

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

    assert "enlace de confirmación" in registration.message
    assert "token" not in registration.model_dump_json()
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
    assert pending.status == "PENDING_ADMIN"
    assert pending.is_active is False
    assert session.query(UserIdentity).filter_by(user_id=pending.id, provider="google").count() == 1
    assert existing.status == "ACTIVE"
    session.close()


def test_pending_admin_login_does_not_issue_token_or_cookie(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    pending = make_user(
        session,
        "pending-admin-gate",
        "BROKER",
        status="PENDING_ADMIN",
        is_active=False,
        email="pending-admin-gate@example.com",
    )
    response = Response()

    with pytest.raises(HTTPException) as blocked:
        login(AuthLoginRequest(email=pending.email, password="user-password"), make_request(), response, session)

    assert blocked.value.status_code == 403
    assert blocked.value.detail == "Aprobación administrativa pendiente"
    assert not [header for header in response.raw_headers if header[0].lower() == b"set-cookie"]
    session.close()


def test_active_status_without_email_confirmation_is_blocked(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    user = make_user(session, "active-unverified-gate", "BROKER", email="active-unverified@example.com")
    user.email_verified = False
    session.commit()
    response = Response()

    with pytest.raises(HTTPException) as blocked:
        login(AuthLoginRequest(email=user.email, password="user-password"), make_request(), response, session)

    assert blocked.value.status_code == 403
    assert blocked.value.detail == "Correo pendiente de confirmación"
    assert not [header for header in response.raw_headers if header[0].lower() == b"set-cookie"]
    session.close()


def test_legacy_active_user_with_blank_status_still_logs_in(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    user = make_user(session, "legacy-active-gate", "BROKER", email="legacy-active@example.com")
    user.status = ""
    session.commit()

    authenticated = login(AuthLoginRequest(email=user.email, password="user-password"), make_request(), Response(), session)

    assert authenticated.access_token
    session.refresh(user)
    assert user.status == "ACTIVE"
    session.close()


def test_inactive_organization_blocks_login_and_existing_token(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    session = create_test_session()
    organization = make_organization(session, "org-inactive-auth", "Organizacion Inactiva")
    user = make_user(session, "org-active-user", "BROKER", organization_id=organization.id)

    authenticated = login(AuthLoginRequest(email=user.email, password="user-password"), make_request(), Response(), session)
    organization.status = "SUSPENDED"
    session.commit()

    with pytest.raises(HTTPException) as blocked_login:
        login(AuthLoginRequest(email=user.email, password="user-password"), make_request(), Response(), session)
    assert blocked_login.value.status_code == 403
    assert blocked_login.value.detail == "Organización inactiva"

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=authenticated.access_token)
    with pytest.raises(HTTPException) as blocked_api:
        get_current_user(make_request(), credentials=credentials, db=session)
    assert blocked_api.value.status_code == 401
    assert blocked_api.value.detail == "Organización inactiva"
    session.close()


def test_mfa_challenge_cannot_bypass_status_change(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AUTH_SECURITY_TEST_MODE", "true")
    session = create_test_session()
    root = make_user(session, "root-mfa-gate", "ROOT", email="root-mfa-gate@example.com")

    first_step = login(AuthLoginRequest(email=root.email, password="user-password"), make_request(), Response(), session)
    assert first_step.mfa_setup_required is True

    setup = start_mfa_setup_from_challenge(
        MfaSetupChallengeStartRequest(challenge_token=first_step.mfa_challenge_token),
        session,
    )
    root.status = "PENDING_ADMIN"
    root.is_active = False
    session.commit()
    code = hotp(setup.secret, totp_counter())

    with pytest.raises(HTTPException) as blocked_confirm:
        confirm_mfa_setup_from_challenge(
            MfaSetupChallengeConfirmRequest(challenge_token=first_step.mfa_challenge_token, code=code),
            make_request("/auth/mfa/setup/confirm-challenge"),
            Response(),
            session,
        )

    assert blocked_confirm.value.status_code == 403
    assert blocked_confirm.value.detail == "Aprobación administrativa pendiente"
    session.close()


def test_mfa_verify_cannot_bypass_suspended_user(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AUTH_SECURITY_TEST_MODE", "true")
    session = create_test_session()
    root = make_user(session, "root-mfa-verify-gate", "ROOT", email="root-mfa-verify-gate@example.com")
    first_step = login(AuthLoginRequest(email=root.email, password="user-password"), make_request(), Response(), session)
    setup = start_mfa_setup_from_challenge(
        MfaSetupChallengeStartRequest(challenge_token=first_step.mfa_challenge_token),
        session,
    )
    root.mfa_secret_encrypted = auth_routes.encrypt_secret(setup.secret)
    root.mfa_enabled = True
    root.status = "SUSPENDED"
    root.is_active = False
    session.commit()

    with pytest.raises(HTTPException) as blocked:
        verify_mfa(
            MfaVerifyRequest(challenge_token=first_step.mfa_challenge_token, code=hotp(setup.secret, totp_counter())),
            make_request("/auth/mfa/verify"),
            Response(),
            session,
        )

    assert blocked.value.status_code == 403
    assert blocked.value.detail == "Usuario suspendido o inactivo"
    session.close()


def test_public_signup_can_be_disabled_at_backend(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "false")
    session = create_test_session()

    with pytest.raises(HTTPException) as blocked:
        register(
            RegisterRequest(
                full_name="Signup Disabled",
                email="signup-disabled@example.com",
                password="broker-password",
            ),
            make_request("/auth/register"),
            session,
        )

    assert blocked.value.status_code == 403
    assert session.query(User).filter(User.email == "signup-disabled@example.com").count() == 0
    assert auth_routes.public_config().public_signup_enabled is False
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
