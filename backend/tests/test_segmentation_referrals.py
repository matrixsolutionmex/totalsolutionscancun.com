from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main  # noqa: F401 - registers the complete model graph for SQLite fixtures
from app.auth.jwt_handler import require_admin_user
from app.database.connection import Base
from app.models import commercial_upgrade_intent, lead, organization, organization_marketplace_link, payment, service_order, service_order_financial, service_order_payment_plan, service_order_quote, service_order_tracking, service_request, service_property, user
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.segmentation_referral import (
    CampaignContact,
    CampaignSuppression,
    ReferralReward,
    SegmentationReferralAuditEvent,
    TechnicianReferral,
)
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.services.segmentation_referral_service import (
    add_suppression,
    campaign_eligibility,
    create_referral,
    recalculate_fit_score,
    release_reward,
    reserve_reward,
    reward_eligibility,
    transition_referral,
    use_reward,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def actor(db, org, role="GERENTE"):
    row = User(organization_id=org.id, username=f"{role.lower()}-{org.id}", email=f"{role.lower()}-{org.id}@x.test",
               password_hash="x", role=role, full_name=role, status="ACTIVE", is_active=True)
    db.add(row)
    db.flush()
    return row


def order(db, org):
    customer = Lead(organization_id=org.id, nome="Customer", email="customer@x.test")
    db.add(customer)
    db.flush()
    row = ServiceOrder(organization_id=org.id, lead_id=customer.id, order_number=f"TS-{org.id}",
                       status="ABERTA", final_service_price=Decimal("2500"))
    db.add(row)
    db.flush()
    return row


def test_fit_score_is_deterministic_and_suppression_wins(db):
    org = Organization(name="Org", slug="org")
    db.add(org)
    db.flush()
    contact = CampaignContact(organization_id=org.id, email="Owner@Example.com", normalized_email="owner@example.com",
                              name="Owner", company="Hotel", segment="HOTEL", country="MX", city="Cancun", language="es")
    db.add(contact)
    db.flush()
    recalculate_fit_score(db, contact)
    assert contact.fit_score == 100
    assert sum(contact.breakdown().values()) == 100
    assert campaign_eligibility(db, contact)["eligible"] is True
    add_suppression(db, org.id, contact.email, "UNSUBSCRIBE")
    assert campaign_eligibility(db, contact) == {"eligible": False, "reason": "SUPPRESSED"}


@pytest.mark.parametrize("reason", ["UNSUBSCRIBE", "COMPLAINT", "MANUAL_BLOCK"])
def test_each_suppression_reason_blocks_a_perfect_contact(db, reason):
    org = Organization(name=f"Org {reason}", slug=f"org-{reason.lower()}")
    db.add(org)
    db.flush()
    contact = CampaignContact(organization_id=org.id, email="owner@example.com", normalized_email="owner@example.com",
                              company="Hotel", segment="HOTEL", country="MX", city="Cancun", language="es")
    db.add(contact)
    db.flush()
    recalculate_fit_score(db, contact)
    add_suppression(db, org.id, contact.email, reason)
    assert contact.fit_score == 100
    assert campaign_eligibility(db, contact)["reason"] == "SUPPRESSED"


def test_referral_lifecycle_creates_capped_data_only_reward(db):
    org = Organization(name="Org", slug="org")
    db.add(org)
    db.flush()
    manager = actor(db, org)
    referrer = Lead(organization_id=org.id, nome="Referrer", email="referrer@example.com")
    db.add(referrer)
    db.flush()
    referral, duplicate = create_referral(db, org.id, "LEAD", referrer.id, "Tech", "tech@example.com", "+52 1 999", manager.id)
    assert referral.status == "PENDING"
    assert duplicate is False
    transition_referral(db, referral, "UNDER_REVIEW", manager.id)
    transition_referral(db, referral, "APPROVED", manager.id)
    reward = db.query(ReferralReward).one()
    assert reward.status == "REWARD_AVAILABLE"
    assert reward.reward_value == Decimal("50.00")
    assert reward.reward_cap_amount == Decimal("1000.00")
    assert db.query(SegmentationReferralAuditEvent).count() >= 3


def test_duplicate_referral_is_flagged_without_deleting_original(db):
    org = Organization(name="Org", slug="org")
    db.add(org)
    db.flush()
    referrer = actor(db, org)
    first, _ = create_referral(db, org.id, "USER", referrer.id, "Tech", "same@example.com", None)
    second, duplicate = create_referral(db, org.id, "USER", referrer.id, "Tech 2", "SAME@example.com", None)
    assert duplicate is True
    assert first.id != second.id
    assert db.query(TechnicianReferral).count() == 2


def test_reward_eligibility_is_scoped_and_does_not_apply_financial_effect(db):
    org_a = Organization(name="A", slug="a")
    org_b = Organization(name="B", slug="b")
    db.add_all([org_a, org_b])
    db.flush()
    manager_a = actor(db, org_a)
    manager_b = actor(db, org_b)
    order_a = order(db, org_a)
    referral, _ = create_referral(db, org_a.id, "LEAD", order_a.lead_id, "Tech", "tech@example.com", None)
    transition_referral(db, referral, "UNDER_REVIEW", manager_a.id)
    transition_referral(db, referral, "APPROVED", manager_a.id)
    db.commit()
    result = reward_eligibility(db, manager_a, order_a.id)
    assert result["eligible"] is True
    assert result["applied"] is False
    assert db.query(payment.Payment).count() == 0
    assert db.query(service_order_financial.ServiceOrderFinancial).count() == 0
    with pytest.raises(HTTPException) as exc:
        reward_eligibility(db, manager_b, order_a.id)
    assert exc.value.status_code == 404


def test_non_admin_role_is_rejected_by_existing_admin_guard(db):
    org = Organization(name="Org", slug="org")
    db.add(org)
    db.flush()
    technician = actor(db, org, role="BROKER")
    with pytest.raises(HTTPException) as exc:
        require_admin_user(technician)
    assert exc.value.status_code == 403


def approved_reward(db, org):
    manager = actor(db, org)
    referrer = Lead(organization_id=org.id, nome="Referrer", email=f"ref-{org.id}@x.test")
    db.add(referrer)
    db.flush()
    referral, _ = create_referral(db, org.id, "LEAD", referrer.id, "Tech", f"tech-{org.id}@x.test", None, manager.id)
    transition_referral(db, referral, "UNDER_REVIEW", manager.id)
    transition_referral(db, referral, "APPROVED", manager.id)
    db.flush()
    service_order = ServiceOrder(organization_id=org.id, lead_id=referrer.id, order_number=f"TS-REF-{org.id}",
                                status="ABERTA", final_service_price=Decimal("2500"))
    db.add(service_order)
    db.flush()
    return manager, referral, db.query(ReferralReward).filter_by(referral_id=referral.id).one(), service_order


@pytest.mark.parametrize(("price", "expected"), [("800", "400.00"), ("3000", "1000.00"), ("10000", "1000.00")])
def test_reward_cap_is_projection_only(db, price, expected):
    org = Organization(name=f"Org {price}", slug=f"org-{price}")
    db.add(org)
    db.flush()
    manager, _, reward, current = approved_reward(db, org)
    current.final_service_price = Decimal(price)
    result = reward_eligibility(db, manager, current.id)
    assert result["potential_discount"] == expected
    assert result["applied"] is False
    assert current.final_service_price == Decimal(price)


def test_cancelled_first_order_does_not_block_second_eligible_order(db):
    org = Organization(name="Org", slug="org")
    db.add(org)
    db.flush()
    manager, _, _, cancelled = approved_reward(db, org)
    cancelled.status = "CANCELADA"
    second = ServiceOrder(organization_id=org.id, lead_id=cancelled.lead_id, order_number="TS-SECOND",
                          status="ABERTA", final_service_price=Decimal("2000"))
    db.add(second)
    db.flush()
    result = reward_eligibility(db, manager, second.id)
    assert result["eligible"] is True


def test_first_eligible_order_only_and_reward_state_is_idempotent(db):
    org = Organization(name="Org", slug="org")
    db.add(org)
    db.flush()
    manager, _, reward, first = approved_reward(db, org)
    second = ServiceOrder(organization_id=org.id, lead_id=first.lead_id, order_number="TS-SECOND",
                          status="ABERTA", final_service_price=Decimal("2000"))
    db.add(second)
    db.flush()
    assert reward_eligibility(db, manager, first.id)["eligible"] is True
    assert reward_eligibility(db, manager, second.id)["reason"] == "FIRST_ELIGIBLE_ORDER_ALREADY_EXISTS"
    reserve_reward(db, manager, reward.id, first.id)
    with pytest.raises(HTTPException) as exc:
        reserve_reward(db, manager, reward.id, second.id)
    assert exc.value.status_code == 409
    release_reward(db, manager, reward.id)
    reserve_reward(db, manager, reward.id, first.id)
    use_reward(db, manager, reward.id, first.id)
    with pytest.raises(HTTPException):
        reserve_reward(db, manager, reward.id, second.id)
    events = {event.event_type for event in db.query(SegmentationReferralAuditEvent).all()}
    assert {"REWARD_CREATED", "REWARD_AVAILABLE", "REWARD_RESERVED", "REWARD_RELEASED", "REWARD_USED"} <= events


def test_cross_org_referrer_is_rejected(db):
    org_a = Organization(name="A", slug="a")
    org_b = Organization(name="B", slug="b")
    db.add_all([org_a, org_b])
    db.flush()
    referrer_b = Lead(organization_id=org_b.id, nome="B", email="b@example.com")
    db.add(referrer_b)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        create_referral(db, org_a.id, "LEAD", referrer_b.id, "Tech", "tech@example.com", None)
    assert exc.value.status_code == 403
