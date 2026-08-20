from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main as _app_main  # noqa: F401
from app.core.security import hash_password
from app.database.connection import Base
from app.models.auth_security import AuthAuditEvent
from app.models.commercial_subscription import CommercialSubscription
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.models.user_commercial_profile import UserCommercialProfile
from app.services.entitlement_service import current_plan
from app.services.legacy_workspace_migration_service import legacy_workspace_candidates, migrate_legacy_user_to_primary
from app.services.marketplace_service import assign_opportunity, list_opportunities
from app.routes.admin_routes import LegacyMigrationRequest, admin_migrate_legacy_user


@pytest.fixture()
def migration_db(monkeypatch):
    primary_slug = f"total-solutions-cancun-{uuid4().hex[:8]}"
    monkeypatch.setenv("PLATFORM_PRIMARY_ORGANIZATION_SLUG", primary_slug)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session, primary_slug
    finally:
        session.close()


def make_user(db, *, username, organization_id, role="BROKER", plan="FREE", manager_id=None):
    user = User(
        username=username,
        email=f"{username}@example.com",
        full_name=username.replace("-", " ").title(),
        password_hash=hash_password("password"),
        organization_id=organization_id,
        role=role,
        manager_id=manager_id,
        plan=plan,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.flush()
    return user


def setup_realistic_case(db, primary_slug, *, magno_role="GERENTE"):
    primary = Organization(name="Total Solutions", slug=primary_slug, is_platform_owner=True, plan="BUSINESS")
    legacy = Organization(name="Magno A B", slug=f"magno-a-b-{uuid4().hex[:8]}", plan="PRO")
    db.add_all([primary, legacy])
    db.flush()
    primary_sub = CommercialSubscription(organization_id=primary.id, plan="BUSINESS", status="ACTIVE")
    legacy_sub = CommercialSubscription(organization_id=legacy.id, plan="PRO", status="ACTIVE")
    db.add_all([primary_sub, legacy_sub])
    root = make_user(db, username="root", organization_id=primary.id, role="ROOT")
    magno = make_user(db, username="magnoalvesbrasil", organization_id=legacy.id, role=magno_role)
    magno.email = "magnoalvesbrasil@proton.me"
    profile = UserCommercialProfile(user_id=magno.id, plan="PRO", status="ACTIVE", source="ROOT_ADMIN", granted_by_user_id=root.id)
    db.add(profile)
    for index in range(15):
        db.add(ServiceOpportunity(public_id=f"MKT-LEGACY-{index}", organization_id=primary.id, source="MARKETPLACE", service_type="MANUTENCAO", city="Cancún"))
    db.commit()
    return primary, legacy, root, magno, primary_sub, legacy_sub


def test_legacy_supervisor_migration_preserves_identity_plan_and_moves_marketplace_scope(migration_db):
    db, primary_slug = migration_db
    primary, legacy, root, magno, primary_sub, legacy_sub = setup_realistic_case(db, primary_slug)
    assert list_opportunities(db, magno) == []
    candidates = legacy_workspace_candidates(db)
    assert [item["user_id"] for item in candidates] == [magno.id]
    original_id = magno.id
    original_email = magno.email

    migrate_legacy_user_to_primary(db, user_id=magno.id, actor=root)
    db.refresh(magno)
    db.refresh(legacy)
    db.refresh(primary_sub)
    db.refresh(legacy_sub)
    assert magno.id == original_id
    assert magno.email == original_email
    assert magno.organization_id == primary.id
    assert magno.role == "GERENTE"
    assert magno.status == "ACTIVE"
    assert current_plan(db, magno) == "PRO"
    assert primary_sub.plan == "BUSINESS"
    assert legacy_sub.plan == "PRO"
    assert legacy.status == "ORPHANED_ONBOARDING"
    assert db.query(Organization).filter_by(id=legacy.id).count() == 1
    assert len(list_opportunities(db, magno)) == 15
    assert db.query(AuthAuditEvent).filter_by(event_type="LEGACY_USER_MIGRATED_TO_PRIMARY_ORGANIZATION").count() == 1


def test_real_magno_broker_pro_independent_state_is_migration_candidate(migration_db):
    db, primary_slug = migration_db
    primary, legacy, root, magno, *_ = setup_realistic_case(db, primary_slug, magno_role="BROKER")
    magno.onboarding_source = "INDEPENDENT"
    db.commit()
    candidates = legacy_workspace_candidates(db)
    assert len(candidates) == 1
    assert candidates[0]["user_id"] == magno.id
    assert candidates[0]["role"] == "BROKER"
    assert candidates[0]["individual_plan"] == "PRO"
    migrate_legacy_user_to_primary(db, user_id=magno.id, actor=root)
    db.refresh(magno)
    assert magno.organization_id == primary.id
    assert magno.role == "BROKER"
    assert current_plan(db, magno) == "PRO"


def test_legacy_migration_is_root_only_and_blocks_real_data(migration_db):
    db, primary_slug = migration_db
    primary, legacy, root, magno, *_ = setup_realistic_case(db, primary_slug)
    with pytest.raises(HTTPException) as non_root:
        migrate_legacy_user_to_primary(db, user_id=magno.id, actor=magno)
    assert non_root.value.status_code == 403
    db.add(Lead(organization_id=legacy.id, nome="Cliente real"))
    db.commit()
    assert legacy_workspace_candidates(db) == []
    with pytest.raises(HTTPException) as blocked:
        migrate_legacy_user_to_primary(db, user_id=magno.id, actor=root)
    assert blocked.value.status_code == 409
    db.refresh(magno)
    assert magno.organization_id == legacy.id


def test_legacy_route_requires_explicit_root_confirmation(migration_db):
    db, primary_slug = migration_db
    _, _, root, magno, *_ = setup_realistic_case(db, primary_slug)
    with pytest.raises(HTTPException) as missing_confirmation:
        admin_migrate_legacy_user(magno.id, LegacyMigrationRequest(confirm=False), db, root)
    assert missing_confirmation.value.status_code == 400
    db.refresh(magno)
    assert magno.organization_id != root.organization_id
    result = admin_migrate_legacy_user(magno.id, LegacyMigrationRequest(confirm=True, reason="Caso legado confirmado"), db, root)
    assert result["organization_id"] == root.organization_id


def test_migrated_supervisor_can_assign_primary_opportunity_to_free_team_member(migration_db):
    db, primary_slug = migration_db
    primary, legacy, root, magno, *_ = setup_realistic_case(db, primary_slug)
    migrate_legacy_user_to_primary(db, user_id=magno.id, actor=root)
    technician = make_user(db, username="new-technician", organization_id=primary.id, role="BROKER", manager_id=magno.id)
    db.add(UserCommercialProfile(user_id=technician.id, plan="FREE", status="ACTIVE", source="ONBOARDING"))
    db.commit()
    result = assign_opportunity(db, magno, "MKT-LEGACY-0", technician.id)
    opportunity = db.query(ServiceOpportunity).filter_by(public_id="MKT-LEGACY-0").one()
    assert opportunity.claimed_by_user_id == technician.id
    assert result["opportunity"]["status"] == "CLAIMED"
