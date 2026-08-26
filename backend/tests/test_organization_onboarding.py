from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.organization import create_independent_organization, get_platform_primary_organization
from app.core.security import hash_password
from app.database.connection import Base
from app.models.commercial_subscription import CommercialSubscription
from app.models.organization import Organization
from app.models.organization_invitation import OrganizationInvitation
from app.models.referral_attribution import ReferralAttribution
from app.models.organization_marketplace_link import OrganizationMarketplaceLink
from app.models.auth_security import AuthRateLimit, UserSession
from app.models.auth_security import AuthAuditEvent
from app.models.notification import EmailOutbox, WebPushSubscription
from app.models.user_lifecycle import UserLifecycleEvent
from app.models.user import User
from app.models import service_order, service_property
from app.services.entitlement_service import current_plan, ensure_user_commercial_profile
from app.services.organization_onboarding_service import accept_invitation, create_invitation, record_referral
from app.services.organization_provisioning_service import provision_organization
from app.services import notification_service
from app import main as _app_main  # registers the application's relationship models
from app.auth.routes import login, register
from app.routes.admin_routes import PlatformRoleRequest, PlatformSupervisorRequest, admin_platform_assign_supervisor, admin_platform_change_role, admin_platform_user_access_diagnostic, admin_resend_organization_invitation, approve_pending_user, pending_onboarding_users, pending_users
from app.routes.organization_routes import current_team, preview_organization_invitation
from app.routes.user_routes import update_user
from app.schemas.auth_schema import AuthLoginRequest, RegisterRequest, UserApprovalRequest
from app.schemas.user_schema import UserUpdate


@pytest.fixture
def onboarding_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Organization.__table__,
            User.__table__,
            CommercialSubscription.__table__,
            OrganizationInvitation.__table__,
            ReferralAttribution.__table__,
            UserSession.__table__,
            AuthRateLimit.__table__,
            AuthAuditEvent.__table__,
            WebPushSubscription.__table__,
            EmailOutbox.__table__,
            UserLifecycleEvent.__table__,
            OrganizationMarketplaceLink.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def make_user(db, *, username, organization_id=None, role="BROKER", email=None):
    user = User(
        username=username,
        email=email or f"{username}@example.com",
        password_hash=hash_password("password"),
        organization_id=organization_id,
        role=role,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.flush()
    return user


def login_request():
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/auth/login",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"user-agent", b"pytest")],
        "client": ("127.0.0.1", 50000),
    })


def test_independent_workspace_is_free_and_does_not_inherit_paid_plan(onboarding_db):
    db = onboarding_db
    paid = create_independent_organization(db, name="Empresa PRO")
    paid.plan = "PRO"
    db.query(CommercialSubscription).filter_by(organization_id=paid.id).one().plan = "PRO"
    independent = create_independent_organization(db, name="Rebeca")
    user = make_user(db, username="rebeca", organization_id=independent.id)
    db.commit()

    assert independent.id != paid.id
    assert independent.plan == "FREE"
    assert db.query(CommercialSubscription).filter_by(organization_id=independent.id).one().status == "LAUNCH_ACCESS"
    assert current_plan(db, user) == "FREE"


def test_root_provisions_empty_tenant_with_gerente_invitation(onboarding_db):
    db = onboarding_db
    root = make_user(db, username="platform-root", role="ROOT")

    organization, invitation = provision_organization(
        db,
        name="Hotel Riviera Maya",
        slug=None,
        country="MX",
        language="es",
        currency="MXN",
        timezone="America/Cancun",
        plan="FREE",
        manager_full_name="Ana Gerente",
        manager_email="ana@hotel.example",
        invited_by=root,
    )
    db.commit()

    assert organization.slug == "hotel-riviera-maya"
    assert organization.status == "ACTIVE"
    assert invitation.role == "GERENTE"
    assert invitation.organization_id == organization.id
    outbox = db.query(EmailOutbox).filter_by(invitation_id=invitation.id).one()
    assert outbox.status == "PENDING"
    assert outbox.body_text == ""
    assert outbox.body_html == ""
    assert outbox.to_email == "ana@hotel.example"
    assert db.query(User).filter(User.organization_id == organization.id).count() == 0
    assert db.query(CommercialSubscription).filter_by(organization_id=organization.id).one().plan == "FREE"
    link = db.query(OrganizationMarketplaceLink).filter_by(organization_id=organization.id, slug="default").one()
    assert link.active is True
    assert link.visibility_scope == "ORGANIZATION"


def test_public_invitation_preview_exposes_only_onboarding_fields(onboarding_db):
    db = onboarding_db
    root = make_user(db, username="preview-root", role="ROOT")
    organization = create_independent_organization(db, name="Preview Company")
    invitation, raw_token = create_invitation(
        db,
        organization=organization,
        invited_by=root,
        invited_email="manager@preview.example",
        role="GERENTE",
    )

    payload = preview_organization_invitation(raw_token, db)

    assert payload["organization_name"] == "Preview Company"
    assert payload["invited_email"] == "manager@preview.example"
    assert payload["role"] == "GERENTE"
    assert "organization_id" not in payload
    assert "invitation_id" not in payload
    assert "token_hash" not in payload


def test_invitation_registration_rejects_email_substitution(onboarding_db):
    db = onboarding_db
    root = make_user(db, username="email-lock-root", role="ROOT")
    organization = create_independent_organization(db, name="Email Lock Company")
    _, raw_token = create_invitation(
        db,
        organization=organization,
        invited_by=root,
        invited_email="invited@email-lock.example",
        role="GERENTE",
    )

    with pytest.raises(HTTPException) as error:
        register(
            RegisterRequest(
                full_name="Wrong Recipient",
                email="other@email-lock.example",
                password="strong-password",
                invite_token=raw_token,
            ),
            login_request(),
            db,
        )
    assert error.value.status_code == 403
    assert db.query(User).filter(User.email == "other@email-lock.example").first() is None


def test_provisioning_rolls_back_if_manager_invitation_fails(onboarding_db, monkeypatch):
    db = onboarding_db
    root = make_user(db, username="rollback-root", role="ROOT")

    def fail_invitation(*args, **kwargs):
        raise RuntimeError("invitation failure")

    monkeypatch.setattr("app.services.organization_provisioning_service.create_invitation", fail_invitation)
    with pytest.raises(RuntimeError):
        provision_organization(
            db,
            name="Rollback Company",
            slug="rollback-company",
            country="MX",
            language="es",
            currency="MXN",
            timezone="America/Cancun",
            plan="FREE",
            manager_full_name="Rollback Manager",
            manager_email="rollback@hotel.example",
            invited_by=root,
        )
    db.rollback()
    assert db.query(Organization).filter_by(slug="rollback-company").first() is None


def test_invitation_outbox_delivers_without_persisting_raw_token_and_can_be_resent(onboarding_db, monkeypatch):
    db = onboarding_db
    root = make_user(db, username="delivery-root", role="ROOT")
    organization, invitation = provision_organization(
        db,
        name="Delivery Company",
        slug="delivery-company",
        country="MX",
        language="es",
        currency="MXN",
        timezone="America/Cancun",
        plan="FREE",
        manager_full_name="Delivery Manager",
        manager_email="delivery@example.com",
        invited_by=root,
    )
    db.commit()
    outbox = db.query(EmailOutbox).filter_by(invitation_id=invitation.id).one()
    original_hash = invitation.token_hash
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.com")
    monkeypatch.setattr(notification_service, "_send_outbox_with_smtp", lambda *_args, **_kwargs: None)

    assert notification_service.process_email_outbox(db) == 1
    db.refresh(outbox)
    db.refresh(invitation)
    assert outbox.status == "SENT"
    assert outbox.body_text == ""
    assert outbox.body_html == ""
    assert outbox.provider == "SMTP"
    assert invitation.token_hash != original_hash

    outbox.last_attempt_at = datetime.utcnow() - timedelta(minutes=2)
    original_delivery_key = outbox.idempotency_key
    db.commit()
    response = admin_resend_organization_invitation(invitation.id, db, root)
    assert response["status"] == "PENDING"
    assert response["recipient"] == "del***@example.com"
    assert "token" not in response
    db.refresh(outbox)
    assert outbox.status == "PENDING"
    assert outbox.body_text == ""
    assert outbox.body_html == ""
    assert outbox.idempotency_key != original_delivery_key


def test_email_outbox_claim_is_exclusive_and_recovers_expired_processing(onboarding_db):
    db = onboarding_db
    item = EmailOutbox(
        organization_id=None,
        recipient_user_id=None,
        to_email="claim@example.com",
        subject="Claim test",
        body_text="body",
        status="PENDING",
        idempotency_key="claim-test-1",
        next_attempt_at=datetime.utcnow(),
    )
    db.add(item)
    db.commit()

    claimed = notification_service._claim_next_email_outbox(db)
    assert claimed is not None
    assert claimed.status == "PROCESSING"
    assert claimed.claimed_at is not None
    assert claimed.attempts == 1

    assert notification_service._claim_next_email_outbox(db) is None

    claimed.claimed_at = datetime.utcnow() - timedelta(seconds=notification_service.EMAIL_OUTBOX_LEASE_SECONDS + 1)
    db.commit()
    recovered = notification_service._claim_next_email_outbox(db)
    assert recovered is not None
    assert recovered.id == claimed.id
    assert recovered.status == "PROCESSING"
    assert recovered.attempts == 2


def test_email_outbox_failure_retries_then_becomes_failed(onboarding_db, monkeypatch):
    db = onboarding_db
    item = EmailOutbox(
        organization_id=None,
        recipient_user_id=None,
        to_email="failure@example.com",
        subject="Failure test",
        body_text="body",
        status="PENDING",
        idempotency_key="failure-test-1",
        next_attempt_at=datetime.utcnow(),
    )
    db.add(item)
    db.commit()
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.com")
    monkeypatch.setattr(notification_service, "_send_outbox_with_smtp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider_timeout")))

    for attempt in range(1, notification_service.EMAIL_OUTBOX_MAX_ATTEMPTS + 1):
        assert notification_service.process_email_outbox(db) == 0
        db.refresh(item)
        expected_status = "FAILED" if attempt == notification_service.EMAIL_OUTBOX_MAX_ATTEMPTS else "RETRY"
        assert item.status == expected_status
        assert item.attempts == attempt
        if item.status == "RETRY":
            item.next_attempt_at = datetime.utcnow()
            db.commit()


def test_root_queue_matches_login_gate_for_non_independent_gerente_pro(onboarding_db):
    db = onboarding_db
    organization = create_independent_organization(db, name="Magno A B")
    organization.plan = "PRO"
    subscription = db.query(CommercialSubscription).filter_by(organization_id=organization.id).one()
    subscription.plan = "PRO"
    root = make_user(db, username="root", organization_id=organization.id, role="ROOT")
    pending = User(
        username="magnoalvesbrasil",
        email="magnoalvesbrasil@proton.me",
        full_name="Magno A B",
        password_hash=hash_password("password"),
        organization_id=organization.id,
        role="GERENTE",
        status="PENDING_ADMIN",
        is_active=False,
        email_verified=True,
        onboarding_source="TEAM",
        plan="PRO",
    )
    db.add(pending)
    db.commit()
    db.refresh(pending)

    with pytest.raises(HTTPException) as blocked:
        login(AuthLoginRequest(email=pending.email, password="password"), login_request(), Response(), db)
    assert blocked.value.status_code == 403
    assert "Aprobación administrativa" in str(blocked.value.detail)

    rows = pending_onboarding_users(db, root)
    row = next(item for item in rows if item["id"] == pending.id)
    assert row["onboarding_source"] == "TEAM"
    assert row["access_block_reason"] == "Aprobación administrativa pendiente"

    with pytest.raises(HTTPException) as role_change:
        admin_platform_change_role(pending.id, PlatformRoleRequest(role="BROKER"), db, root)
    assert role_change.value.status_code == 409
    with pytest.raises(HTTPException) as alternate_role_change:
        update_user(pending.id, UserUpdate(role="BROKER"), db, root)
    assert alternate_role_change.value.status_code == 409

    diagnostic = admin_platform_user_access_diagnostic(pending.id, db, root)
    assert diagnostic == {
        "user_id": pending.id,
        "email": pending.email,
        "email_verified": True,
        "status": "PENDING_ADMIN",
        "role": "GERENTE",
        "is_active": False,
        "onboarding_source": "TEAM",
        "organization_id": organization.id,
        "supervisor": None,
        "access_block_reason": "Aprobación administrativa pendiente",
    }

    original_organization_id = pending.organization_id
    approved = approve_pending_user(
        db,
        pending,
        root,
        UserApprovalRequest(role="GERENTE", organization_mode="EXISTING", organization_id=organization.id),
    )
    db.commit()
    assert approved.status == "ACTIVE"
    assert approved.is_active is True
    assert approved.role == "GERENTE"
    assert approved.organization_id == original_organization_id
    assert approved.plan == "PRO"
    assert current_plan(db, approved) == "PRO"
    assert not any(item["id"] == approved.id for item in pending_onboarding_users(db, root))

    authenticated = login(AuthLoginRequest(email=pending.email, password="password"), login_request(), Response(), db)
    # GERENTE is subject to the existing MFA step; reaching this response proves
    # administrative approval no longer blocks password authentication.
    assert authenticated.mfa_required is True
    assert authenticated.mfa_challenge_token


def test_admin_approval_does_not_bypass_unverified_email(onboarding_db):
    db = onboarding_db
    organization = create_independent_organization(db, name="Pending Email")
    root = make_user(db, username="root-email", organization_id=organization.id, role="ROOT")
    pending = User(
        username="unverified",
        email="unverified@example.com",
        full_name="Unverified",
        password_hash=hash_password("password"),
        organization_id=organization.id,
        role="BROKER",
        status="PENDING_ADMIN",
        is_active=False,
        email_verified=False,
        onboarding_source="TEAM",
    )
    db.add(pending)
    db.commit()

    with pytest.raises(HTTPException) as approval:
        approve_pending_user(
            db,
            pending,
            root,
            UserApprovalRequest(role="BROKER", organization_mode="EXISTING", organization_id=organization.id),
        )
    assert approval.value.status_code == 400
    assert pending.status == "PENDING_ADMIN"
    assert pending.is_active is False

    with pytest.raises(HTTPException) as blocked:
        login(AuthLoginRequest(email=pending.email, password="password"), login_request(), Response(), db)
    assert blocked.value.status_code == 403
    assert "Correo pendiente" in str(blocked.value.detail)


def test_invitation_is_explicit_single_use_and_starts_member_free(onboarding_db):
    db = onboarding_db
    organization = create_independent_organization(db, name="Equipe PRO")
    organization.plan = "PRO"
    db.query(CommercialSubscription).filter_by(organization_id=organization.id).one().plan = "PRO"
    admin = make_user(db, username="admin", organization_id=organization.id, role="ROOT")
    invite, raw_token = create_invitation(db, organization=organization, invited_by=admin, invited_email="member@example.com")
    member = make_user(db, username="member", email="member@example.com")

    accepted = accept_invitation(db, raw_token=raw_token, user=member)
    db.commit()
    assert accepted.status == "ACCEPTED"
    assert member.organization_id == organization.id
    assert current_plan(db, member) == "FREE"
    assert db.query(CommercialSubscription).filter_by(organization_id=organization.id).one().plan == "PRO"
    assert db.query(CommercialSubscription).filter_by(organization_id=organization.id).count() == 1
    with pytest.raises(HTTPException) as reused:
        accept_invitation(db, raw_token=raw_token, user=member)
    assert reused.value.status_code == 409


def test_expired_invitation_and_cross_org_supervisor_are_rejected(onboarding_db):
    db = onboarding_db
    org_a = create_independent_organization(db, name="A")
    org_b = create_independent_organization(db, name="B")
    admin_a = make_user(db, username="admin-a", organization_id=org_a.id, role="ROOT")
    manager_b = make_user(db, username="manager-b", organization_id=org_b.id, role="GERENTE")
    with pytest.raises(HTTPException) as cross_org:
        create_invitation(
            db,
            organization=org_a,
            invited_by=admin_a,
            invited_email="x@example.com",
            supervisor_user_id=manager_b.id,
        )
    assert cross_org.value.status_code == 400

    invite, token = create_invitation(db, organization=org_a, invited_by=admin_a, invited_email="x@example.com")
    invite.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    with pytest.raises(HTTPException) as expired:
        from app.services.organization_onboarding_service import invitation_for_token
        invitation_for_token(db, token)
    assert expired.value.status_code == 410


def test_referral_is_recorded_without_changing_organization(onboarding_db):
    db = onboarding_db
    organization = create_independent_organization(db, name="Referral")
    user = make_user(db, username="referred", organization_id=organization.id)
    record_referral(db, user=user, referral_code="PARTNER-42", referral_email="partner@example.com")
    db.commit()
    attribution = db.query(ReferralAttribution).filter_by(user_id=user.id).one()
    assert attribution.referral_code == "PARTNER-42"
    assert user.organization_id == organization.id


def test_root_sees_global_pending_onboarding_but_manager_does_not(onboarding_db):
    db = onboarding_db
    root_org = create_independent_organization(db, name="Root")
    other_org = create_independent_organization(db, name="Outra")
    root = make_user(db, username="global-root", organization_id=root_org.id, role="ROOT")
    manager = make_user(db, username="local-manager", organization_id=root_org.id, role="GERENTE")
    pending = make_user(db, username="new-independent", organization_id=other_org.id)
    pending.status = "PENDING_ADMIN"
    pending.is_active = False
    pending.onboarding_source = "INDEPENDENT"
    db.commit()

    global_rows = pending_onboarding_users(db, root)
    assert {row["id"] for row in global_rows} == {pending.id}
    assert global_rows[0]["organization_id"] == other_org.id
    assert pending_users(db, manager) == []


def test_global_independent_approval_preserves_workspace_and_free_plan(onboarding_db):
    db = onboarding_db
    root_org = create_independent_organization(db, name="Root")
    workspace = create_independent_organization(db, name="Rebeca Workspace")
    root = make_user(db, username="approval-root", organization_id=root_org.id, role="ROOT")
    pending = make_user(db, username="rebeca-pending", organization_id=workspace.id)
    pending.status = "PENDING_ADMIN"
    pending.is_active = False
    pending.onboarding_source = "INDEPENDENT"
    db.commit()
    workspace_id = workspace.id

    approved = approve_pending_user(
        db,
        pending,
        root,
        UserApprovalRequest(role="BROKER", organization_mode="INDEPENDENT"),
    )
    db.commit()
    db.refresh(approved)
    db.refresh(workspace)
    subscription = db.query(CommercialSubscription).filter_by(organization_id=workspace_id).one()
    assert approved.organization_id == workspace_id
    assert approved.status == "ACTIVE"
    assert workspace.status == "ACTIVE"
    assert workspace.plan == "FREE"
    assert subscription.plan == "FREE"
    assert subscription.status == "LAUNCH_ACCESS"
    assert db.query(Organization).count() == 2


def test_global_assignment_marks_empty_provisional_workspace_without_deleting_it(onboarding_db):
    db = onboarding_db
    root_org = create_independent_organization(db, name="Root")
    provisional = create_independent_organization(db, name="Provisória")
    target = create_independent_organization(db, name="Empresa existente", pending_onboarding=False)
    root = make_user(db, username="assignment-root", organization_id=root_org.id, role="ROOT")
    manager = make_user(db, username="target-manager", organization_id=target.id, role="GERENTE")
    pending = make_user(db, username="assign-me", organization_id=provisional.id)
    pending.status = "PENDING_ADMIN"
    pending.is_active = False
    pending.onboarding_source = "INDEPENDENT"
    db.commit()

    approved = approve_pending_user(
        db,
        pending,
        root,
        UserApprovalRequest(
            role="BROKER",
            organization_mode="EXISTING",
            organization_id=target.id,
            manager_id=manager.id,
        ),
    )
    db.commit()
    db.refresh(provisional)
    assert approved.organization_id == target.id
    assert approved.manager_id == manager.id
    assert provisional.status == "ORPHANED_ONBOARDING"
    assert db.query(Organization).filter_by(id=provisional.id).count() == 1


def test_standard_signup_approval_keeps_existing_primary_flow_unchanged(onboarding_db):
    db = onboarding_db
    primary = Organization(name="Total Solutions", slug="total-solutions-cancun", is_platform_owner=True)
    db.add(primary)
    db.flush()
    db.add(CommercialSubscription(organization_id=primary.id, plan="BUSINESS", status="ACTIVE"))
    root = make_user(db, username="primary-root", organization_id=primary.id, role="ROOT")
    pending = make_user(db, username="standard-tech", organization_id=primary.id)
    pending.status = "PENDING_ADMIN"
    pending.is_active = False
    pending.onboarding_source = "STANDARD"
    ensure_user_commercial_profile(db, pending, plan="FREE", source="PLATFORM_SIGNUP")
    db.commit()

    approved = approve_pending_user(db, pending, root, UserApprovalRequest(role="BROKER", organization_mode="INDEPENDENT"))
    db.commit()
    assert approved.organization_id == primary.id
    assert approved.manager_id is None
    assert current_plan(db, approved) == "FREE"
    assert db.query(Organization).count() == 1


def test_root_can_assign_same_org_supervisor_but_cross_org_is_rejected(onboarding_db):
    db = onboarding_db
    org_a = create_independent_organization(db, name="A")
    org_b = create_independent_organization(db, name="B")
    root = make_user(db, username="root-a", organization_id=org_a.id, role="ROOT")
    manager_a = make_user(db, username="manager-a", organization_id=org_a.id, role="GERENTE")
    technician = make_user(db, username="technician-a", organization_id=org_a.id, role="BROKER")
    manager_b = make_user(db, username="manager-b", organization_id=org_b.id, role="GERENTE")
    db.commit()

    result = admin_platform_assign_supervisor(technician.id, PlatformSupervisorRequest(supervisor_id=manager_a.id), db, root)
    assert result["supervisor_id"] == manager_a.id
    with pytest.raises(HTTPException) as error:
        admin_platform_assign_supervisor(technician.id, PlatformSupervisorRequest(supervisor_id=manager_b.id), db, root)
    assert error.value.status_code == 400


def test_root_promotes_pro_without_changing_plan_or_subscription(onboarding_db):
    db = onboarding_db
    organization = create_independent_organization(db, name="Promotion")
    root = make_user(db, username="promotion-root", organization_id=organization.id, role="ROOT")
    pro_technician = make_user(db, username="pro-tech", organization_id=organization.id, role="BROKER")
    free_technician = make_user(db, username="free-tech", organization_id=organization.id, role="BROKER")
    profile = ensure_user_commercial_profile(db, pro_technician, plan="PRO", source="ROOT_ADMIN", granted_by_user_id=root.id)
    db.commit()
    subscription_plan = db.query(CommercialSubscription).filter_by(organization_id=organization.id).one().plan

    with pytest.raises(HTTPException) as free_error:
        admin_platform_change_role(free_technician.id, PlatformRoleRequest(role="SUPERVISOR"), db, root)
    assert free_error.value.status_code == 403
    promoted = admin_platform_change_role(pro_technician.id, PlatformRoleRequest(role="SUPERVISOR"), db, root)
    db.refresh(pro_technician)
    assert promoted["role"] == "GERENTE"
    assert current_plan(db, pro_technician) == "PRO"
    assert pro_technician.organization_id == organization.id
    assert db.query(CommercialSubscription).filter_by(organization_id=organization.id).one().plan == subscription_plan
    assert profile.plan == "PRO"


def test_supervisor_invitation_creates_free_technician_in_same_team(onboarding_db):
    db = onboarding_db
    organization = create_independent_organization(db, name="Team")
    root = make_user(db, username="team-root", organization_id=organization.id, role="ROOT")
    supervisor = make_user(db, username="team-supervisor", organization_id=organization.id, role="GERENTE")
    ensure_user_commercial_profile(db, supervisor, plan="PRO", source="ROOT_ADMIN", granted_by_user_id=root.id)
    invite, token = create_invitation(db, organization=organization, invited_by=supervisor, invited_email="new-tech@example.com", role="BROKER", supervisor_user_id=supervisor.id)
    pending = make_user(db, username="new-tech@example.com", email="new-tech@example.com")
    accepted = accept_invitation(db, raw_token=token, user=pending)
    db.commit()
    assert accepted.status == "ACCEPTED"
    assert pending.organization_id == organization.id
    assert pending.manager_id == supervisor.id
    assert pending.role == "BROKER"
    assert current_plan(db, pending) == "FREE"
    assert [item["id"] for item in current_team(db, supervisor)] == [pending.id]


def test_demoting_supervisor_with_team_requires_explicit_treatment(onboarding_db):
    db = onboarding_db
    organization = create_independent_organization(db, name="Demotion")
    root = make_user(db, username="demotion-root", organization_id=organization.id, role="ROOT")
    supervisor = make_user(db, username="demotion-supervisor", organization_id=organization.id, role="GERENTE")
    technician = make_user(db, username="demotion-tech", organization_id=organization.id, role="BROKER",)
    supervisor.manager_id = None
    technician.manager_id = supervisor.id
    db.commit()
    with pytest.raises(HTTPException) as error:
        admin_platform_change_role(supervisor.id, PlatformRoleRequest(role="BROKER"), db, root)
    assert error.value.status_code == 409
    result = admin_platform_change_role(supervisor.id, PlatformRoleRequest(role="BROKER", subordinate_action="CLEAR"), db, root)
    db.refresh(technician)
    assert result["role"] == "BROKER"
    assert technician.manager_id is None
