from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.organization import create_independent_organization
from app.core.security import hash_password
from app.database.connection import Base
from app.models.commercial_subscription import CommercialSubscription
from app.models.organization import Organization
from app.models.organization_invitation import OrganizationInvitation
from app.models.referral_attribution import ReferralAttribution
from app.models.user import User
from app.models import service_order, service_property
from app.services.entitlement_service import current_plan
from app.services.organization_onboarding_service import accept_invitation, create_invitation, record_referral
from app import main as _app_main  # registers the application's relationship models


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


def test_invitation_is_explicit_single_use_and_preserves_org_plan(onboarding_db):
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
    assert current_plan(db, member) == "PRO"
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
