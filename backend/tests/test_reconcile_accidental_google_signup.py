import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main as _model_registry  # noqa: F401
from app.database.connection import Base
from app.models.auth_security import UserIdentity
from app.models.commercial_subscription import CommercialSubscription
from app.models.organization import Organization
from app.models.organization_marketplace_link import OrganizationMarketplaceLink
from app.models.user import User
from app.models.user_commercial_profile import UserCommercialProfile
from app.services.organization_marketplace_service import resolve_marketplace_link
from scripts.reconcile_accidental_google_signup import (
    ACCIDENTAL_ORGANIZATION_ID,
    ACCIDENTAL_USER_ID,
    CANONICAL_ORGANIZATION_ID,
    CANONICAL_USER_ID,
    apply_reconciliation,
    audit,
    validate_apply_preconditions,
)


def make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def seed_residual_session():
    session = make_session()
    canonical_org = Organization(
        id=CANONICAL_ORGANIZATION_ID,
        name="Canonical",
        slug="canonical",
        plan="BUSINESS",
        status="ACTIVE",
        is_platform_owner=True,
    )
    residual_org = Organization(
        id=ACCIDENTAL_ORGANIZATION_ID,
        name="Residual",
        slug="residual",
        plan="FREE",
        status="ACTIVE",
        is_platform_owner=False,
    )
    canonical = User(
        id=CANONICAL_USER_ID,
        organization_id=CANONICAL_ORGANIZATION_ID,
        username="weverton.admin",
        email="root@example.com",
        password_hash="hash",
        role="ROOT",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
        mfa_enabled=True,
    )
    accidental = User(
        id=ACCIDENTAL_USER_ID,
        organization_id=ACCIDENTAL_ORGANIZATION_ID,
        username="accidental",
        email=None,
        password_hash="hash",
        role="BROKER",
        status="ACTIVE",
        is_active=True,
        session_version=2,
    )
    session.add_all([canonical_org, residual_org, canonical, accidental])
    session.flush()
    session.add_all([
        UserIdentity(user_id=CANONICAL_USER_ID, organization_id=CANONICAL_ORGANIZATION_ID, provider="google", provider_subject="root-sub"),
        CommercialSubscription(organization_id=CANONICAL_ORGANIZATION_ID, plan="BUSINESS", status="ACTIVE", provider="MOCK"),
        CommercialSubscription(organization_id=ACCIDENTAL_ORGANIZATION_ID, plan="FREE", status="LAUNCH_ACCESS", provider="MOCK"),
        UserCommercialProfile(user_id=ACCIDENTAL_USER_ID, plan="FREE", status="ACTIVE", source="PLATFORM_SIGNUP"),
        OrganizationMarketplaceLink(
            organization_id=ACCIDENTAL_ORGANIZATION_ID,
            name="Marketplace principal",
            slug="default",
            source_code="MARKETPLACE_LINK",
            visibility_scope="ORGANIZATION",
            active=True,
        ),
    ])
    session.commit()
    return session


def test_active_residual_is_reconciled_and_marketplace_bootstrap_is_disabled():
    session = seed_residual_session()
    before = audit(session)
    assert before["accidental_organization_status"] == "ACTIVE"
    assert before["operational_organization9_count"] == 0
    assert before["marketplace_links"][0]["slug"] == "default"

    apply_reconciliation(session, before)
    session.expire_all()
    assert session.get(User, 38).status == "ARCHIVED"
    assert session.get(User, 38).is_active is False
    assert session.get(Organization, 9).status == "ORPHANED_ONBOARDING"
    assert session.query(OrganizationMarketplaceLink).one().active is False
    with pytest.raises(HTTPException) as blocked:
        resolve_marketplace_link(session, "residual")
    assert blocked.value.status_code == 404
    assert session.query(CommercialSubscription).filter_by(organization_id=9).one().status == "CANCELLED"
    assert session.query(UserCommercialProfile).filter_by(user_id=38).one().status == "ACTIVE"
    assert session.get(User, 1).status == "ACTIVE"
    assert session.get(User, 1).organization_id == 1
    assert session.query(UserIdentity).filter_by(provider_subject="root-sub").one().user_id == 1

    second = audit(session)
    apply_reconciliation(session, second)
    assert session.query(User).filter_by(id=38).one().status == "ARCHIVED"
    assert session.query(OrganizationMarketplaceLink).one().active is False
    session.close()


def test_second_user_aborts_before_any_mutation():
    session = seed_residual_session()
    session.add(User(
        organization_id=9,
        username="second",
        email="second@example.com",
        password_hash="hash",
        role="BROKER",
        status="ACTIVE",
        is_active=True,
    ))
    session.commit()
    report = audit(session)
    try:
        validate_apply_preconditions(report)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected second-user guard")
    assert session.get(User, 38).status == "ACTIVE"
    assert session.get(Organization, 9).status == "ACTIVE"
    assert session.query(OrganizationMarketplaceLink).one().active is True
    session.close()


def test_operational_reference_aborts_before_any_mutation():
    session = seed_residual_session()
    report = audit(session)
    report["operational_organization9_count"] = 1
    try:
        validate_apply_preconditions(report)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected operational-reference guard")
    assert session.get(User, 38).status == "ACTIVE"
    assert session.get(Organization, 9).status == "ACTIVE"
    session.close()


def test_platform_owner_and_external_subscription_are_blocked():
    session = seed_residual_session()
    report = audit(session)
    report["accidental_organization_is_platform_owner"] = True
    try:
        validate_apply_preconditions(report)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected platform-owner guard")
    report = audit(session)
    report["subscription"]["external_reference_present"] = True
    try:
        validate_apply_preconditions(report)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected external-reference guard")
    assert session.get(User, 38).status == "ACTIVE"
    assert session.get(Organization, 9).status == "ACTIVE"
    session.close()


@pytest.mark.parametrize("status", ["PENDING_ONBOARDING", "ORPHANED_ONBOARDING"])
def test_residual_workspace_statuses_are_supported(status):
    session = seed_residual_session()
    session.get(Organization, 9).status = status
    session.commit()
    report = audit(session)
    validate_apply_preconditions(report)
    session.close()


@pytest.mark.parametrize(
    "field",
    [
        "payments.organization_id",
        "platform_ledger_entries.organization_id",
        "service_order_ledger_entries.organization_id",
        "service_orders.organization_id",
        "service_requests.organization_id",
    ],
)
def test_financial_or_operational_guard_aborts(field):
    session = seed_residual_session()
    report = audit(session)
    report["organization9_counts"][field] = 1
    report["operational_organization9_count"] = 1
    with pytest.raises(RuntimeError):
        validate_apply_preconditions(report)
    assert session.get(User, 38).status == "ACTIVE"
    assert session.get(Organization, 9).status == "ACTIVE"
    session.close()


def test_non_bootstrap_marketplace_link_aborts():
    session = seed_residual_session()
    link = session.query(OrganizationMarketplaceLink).one()
    link.slug = "campaign"
    session.commit()
    with pytest.raises(RuntimeError):
        validate_apply_preconditions(audit(session))
    assert link.active is True
    assert session.get(Organization, 9).status == "ACTIVE"
    session.close()


def test_non_mock_subscription_aborts():
    session = seed_residual_session()
    subscription = session.query(CommercialSubscription).filter_by(organization_id=9).one()
    subscription.provider = "STRIPE"
    session.commit()
    with pytest.raises(RuntimeError):
        validate_apply_preconditions(audit(session))
    assert subscription.status == "LAUNCH_ACCESS"
    assert session.get(Organization, 9).status == "ACTIVE"
    session.close()
