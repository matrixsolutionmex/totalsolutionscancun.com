from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
# Import the application model registry so all legacy foreign-key targets are present.
import app.main  # noqa: F401,E402
from app.main import STARTUP_MANUAL_MIGRATION_TABLES, startup_managed_tables
from app.routes.organization_membership_routes import require_network_schema
from app.models.organization import Organization
from app.models.organization_membership import NetworkFeePolicy, OrganizationMembership
from app.models.service_order import ServiceOrder
from app.models.technician_transfer import TechnicianTransferRequest
from app.models.user import User
from app.services.organization_membership_service import (
    approve_exclusive_transfer,
    backfill_memberships,
    apply_membership_backfill,
    get_active_memberships,
    membership_schema_available,
    plan_membership_backfill,
    transfer_blockers,
)
from app.models.user_lifecycle import UserLifecycleEvent
from scripts import transition_user_role


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


def make_org(db, slug: str, name: str, status: str = "ACTIVE") -> Organization:
    row = Organization(name=name, slug=slug, status=status)
    db.add(row)
    db.flush()
    return row


def make_user(db, org: Organization, username: str, role: str = "BROKER") -> User:
    row = User(
        organization_id=org.id,
        username=username,
        email=f"{username}@example.test",
        password_hash="test-hash",
        role=role,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
        registered_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def test_backfill_is_idempotent_and_preserves_legacy_tenant(db):
    org = make_org(db, "org-a", "Organization A")
    user = make_user(db, org, "tech-a")
    legacy_org_id = user.organization_id

    assert backfill_memberships(db) == 1
    db.commit()
    assert backfill_memberships(db) == 0
    membership = db.query(OrganizationMembership).one()

    assert membership.user_id == user.id
    assert membership.organization_id == org.id
    assert membership.status == "ACTIVE"
    assert membership.is_primary is True
    assert membership.is_operational is True
    assert user.organization_id == legacy_org_id
    created_at = membership.created_at
    updated_at = membership.updated_at
    assert backfill_memberships(db) == 0
    db.commit()
    db.refresh(membership)
    assert membership.created_at == created_at
    assert membership.updated_at == updated_at


def test_non_operational_organization_gets_suspended_membership(db):
    org = make_org(db, "org-orphaned", "Orphaned", status="ORPHANED_ONBOARDING")
    user = make_user(db, org, "orphaned-user")

    assert backfill_memberships(db) == 1
    db.commit()
    membership = db.query(OrganizationMembership).filter_by(user_id=user.id).one()

    assert membership.status == "SUSPENDED"
    assert membership.is_operational is False
    assert get_active_memberships(db, user.id) == []


def test_only_one_active_primary_membership_per_user(db):
    source = make_org(db, "primary-source", "Primary source")
    target = make_org(db, "primary-target", "Primary target")
    user = make_user(db, source, "single-primary")
    backfill_memberships(db)
    db.add(OrganizationMembership(
        user_id=user.id,
        organization_id=target.id,
        membership_type="NETWORK_PARTNER",
        role="NETWORK_PARTNER",
        status="ACTIVE",
        is_primary=True,
        is_operational=True,
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_network_membership_can_coexist_with_primary_membership(db):
    source = make_org(db, "source", "Source")
    target = make_org(db, "target", "Total Solutions")
    user = make_user(db, source, "tech-network")
    backfill_memberships(db)
    request = TechnicianTransferRequest(
        user_id=user.id,
        from_organization_id=source.id,
        to_organization_id=target.id,
        requested_transfer_type="NETWORK_MEMBERSHIP",
    )
    db.add(request)
    db.flush()
    membership = OrganizationMembership(
        user_id=user.id,
        organization_id=target.id,
        membership_type="NETWORK_PARTNER",
        role="NETWORK_PARTNER",
        status="ACTIVE",
        is_primary=False,
        is_operational=True,
        joined_at=datetime.utcnow(),
    )
    db.add(membership)
    db.commit()

    active = get_active_memberships(db, user.id)
    assert {(row.organization_id, row.is_primary) for row in active} == {(source.id, True), (target.id, False)}
    assert user.organization_id == source.id


def test_open_order_blocks_exclusive_transfer_without_changing_history(db):
    source = make_org(db, "source-blocked", "Source")
    target = make_org(db, "target-blocked", "Target")
    user = make_user(db, source, "tech-blocked")
    backfill_memberships(db)
    order = ServiceOrder(
        organization_id=source.id,
        lead_id=1,
        order_number="TS-MEMBERSHIP-BLOCKER",
        status="EN_CAMINO",
        responsible_user_id=user.id,
    )
    db.add(order)
    db.flush()
    blockers = transfer_blockers(db, user.id, source.id)
    assert any(item["code"] == "OPEN_SERVICE_ORDER" for item in blockers)

    request = TechnicianTransferRequest(
        user_id=user.id,
        from_organization_id=source.id,
        to_organization_id=target.id,
        requested_transfer_type="EXCLUSIVE_TRANSFER",
    )
    db.add(request)
    db.flush()
    root = make_user(db, source, "root-reviewer", role="ROOT")
    membership, result = approve_exclusive_transfer(db, request, root)
    assert membership is None
    assert result
    assert request.status == "TRANSFER_PENDING_BLOCKED"
    assert user.organization_id == source.id
    assert order.organization_id == source.id
    assert db.query(OrganizationMembership).filter_by(organization_id=target.id).count() == 0


def test_exclusive_transfer_exits_old_membership_and_keeps_legacy_context(db):
    source = make_org(db, "source-transfer", "Source")
    target = make_org(db, "target-transfer", "Target")
    user = make_user(db, source, "tech-transfer")
    backfill_memberships(db)
    request = TechnicianTransferRequest(
        user_id=user.id,
        from_organization_id=source.id,
        to_organization_id=target.id,
        requested_transfer_type="EXCLUSIVE_TRANSFER",
        terms_version="network-v1",
        terms_accepted_at=datetime.utcnow(),
    )
    db.add(request)
    db.flush()
    root = make_user(db, source, "root-transfer", role="ROOT")
    membership, blockers = approve_exclusive_transfer(db, request, root)
    db.commit()

    assert blockers == []
    assert membership.organization_id == target.id
    assert membership.status == "ACTIVE"
    assert membership.terms_version == "network-v1"
    assert membership.terms_accepted_at is not None
    assert request.status == "APPROVED"
    old = db.query(OrganizationMembership).filter_by(user_id=user.id, organization_id=source.id).one()
    assert old.status == "EXITED"
    assert old.is_operational is False
    assert user.organization_id == source.id


def test_fee_policy_is_configuration_only(db):
    org = make_org(db, "org-fee", "Fee Org")
    policy = NetworkFeePolicy(
        organization_id=org.id,
        country="MX",
        service_origin="NETWORK",
        platform_fee_rate="15.00",
        currency="MXN",
        status="ACTIVE",
    )
    db.add(policy)
    db.commit()
    assert policy.platform_fee_rate == "15.00"
    assert db.query(ServiceOrder).count() == 0


def test_explicit_root_policy_is_required_and_idempotent(db):
    org = make_org(db, "org-policy", "Policy Org")
    owner = make_user(db, org, "owner-root", role="ROOT")
    contingency = make_user(db, org, "contingency-root", role="ROOT")
    admin = make_user(db, org, "admin-root", role="ROOT")
    manager = make_user(db, org, "manager", role="GERENTE")
    technician = make_user(db, org, "technician", role="BROKER")

    plan = plan_membership_backfill(
        db,
        owner_user_ids={owner.id, contingency.id},
        admin_user_ids={admin.id},
    )
    assert db.query(OrganizationMembership).count() == 0
    desired = {item["user_id"]: item["role"] for item in plan if item["action"] == "CREATE"}
    assert desired == {
        owner.id: "OWNER",
        contingency.id: "OWNER",
        admin.id: "ADMIN",
        manager.id: "ADMIN",
        technician.id: "TECHNICIAN",
    }

    counts = apply_membership_backfill(db, plan)
    db.commit()
    assert counts == {"created": 5, "updated": 0, "unchanged": 0}
    assert {row.role for row in db.query(OrganizationMembership).all()} == {
        "OWNER", "ADMIN", "TECHNICIAN"
    }

    second = plan_membership_backfill(
        db,
        owner_user_ids={owner.id, contingency.id},
        admin_user_ids={admin.id},
    )
    assert all(item["action"] == "UNCHANGED" for item in second)
    assert apply_membership_backfill(db, second) == {"created": 0, "updated": 0, "unchanged": 5}
    db.rollback()


def test_unclassified_root_fails_closed_without_writes(db):
    org = make_org(db, "org-unclassified", "Unclassified Org")
    root = make_user(db, org, "unclassified-root", role="ROOT")

    with pytest.raises(ValueError, match="requires explicit owner/admin classification"):
        plan_membership_backfill(db, owner_user_ids=set(), admin_user_ids=set())
    db.rollback()
    assert db.query(OrganizationMembership).count() == 0
    assert root.role == "ROOT"


def test_inactive_user_and_orphaned_org_are_suspended_by_explicit_plan(db):
    org = make_org(db, "org-suspended", "Suspended Org", status="ORPHANED_ONBOARDING")
    user = make_user(db, org, "archived-user", role="BROKER")
    user.status = "ARCHIVED"
    user.is_active = False

    plan = plan_membership_backfill(db)
    assert plan[0]["status"] == "SUSPENDED"
    assert plan[0]["is_operational"] is False
    apply_membership_backfill(db, plan)
    db.commit()
    row = db.query(OrganizationMembership).one()
    assert row.status == "SUSPENDED"
    assert row.is_operational is False


def test_role_transition_tool_is_guarded_idempotent_and_audited(db, monkeypatch):
    org = make_org(db, "org-transition", "Transition Org")
    actor = make_user(db, org, "transition-root", role="ROOT")
    target = make_user(db, org, "transition-target", role="ROOT")
    target_id = target.id
    org_id = org.id
    actor_id = actor.id
    db.commit()
    monkeypatch.setattr(transition_user_role, "SessionLocal", lambda: db)

    args = [
        "--dry-run", "--user-id", str(target_id), "--expected-role", "ROOT",
        "--new-role", "GERENTE", "--expected-organization-id", str(org_id),
        "--actor-user-id", str(actor_id),
    ]
    assert transition_user_role.main(args) == 0
    assert db.get(User, target_id).role == "ROOT"
    assert db.query(UserLifecycleEvent).count() == 0

    assert transition_user_role.main(["--apply", *args[1:]]) == 0
    db.flush()
    assert db.get(User, target_id).role == "GERENTE"
    assert db.query(UserLifecycleEvent).filter_by(event_type="ROLE_TRANSITION").count() == 1

    assert transition_user_role.main(["--apply", *args[1:]]) == 0
    assert db.query(UserLifecycleEvent).filter_by(event_type="ROLE_TRANSITION").count() == 1


def test_network_migration_is_structural_and_startup_does_not_backfill():
    from pathlib import Path

    migration = Path("backend/migrations/083_organization_membership_foundation.sql").read_text()
    startup = Path("backend/app/main.py").read_text()
    assert "INSERT INTO organization_memberships" not in migration
    assert "backfill_memberships" not in startup
    assert "organization_membership_router" in startup


def test_startup_create_all_excludes_all_manual_migration_tables():
    managed_names = {table.name for table in startup_managed_tables()}
    expected_manual = {
        "identity_verifications",
        "identity_verification_attempts",
        "identity_verification_events",
        "identity_human_review_decisions",
        "stripe_webhook_events",
        "stripe_payment_adjustments",
        "technician_earnings",
        "technician_earning_events",
        "network_fee_policies",
        "organization_memberships",
        "technician_transfer_requests",
    }
    assert expected_manual <= STARTUP_MANUAL_MIGRATION_TABLES
    assert managed_names.isdisjoint(expected_manual)


def test_missing_network_schema_is_detected_fail_closed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session = sessionmaker(bind=engine)()
    try:
        assert membership_schema_available(session) is False
    finally:
        session.close()
        engine.dispose()


def test_network_route_guard_returns_sanitized_503_without_schema():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session = sessionmaker(bind=engine)()
    try:
        with pytest.raises(Exception) as caught:
            require_network_schema(session)
        assert caught.value.status_code == 503
        assert caught.value.detail == "Membership operacional indisponível"
    finally:
        session.close()
        engine.dispose()
