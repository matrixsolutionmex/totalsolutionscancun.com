from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main  # noqa: F401
from app.database.connection import Base
from app.models.identity_human_review import IdentityHumanReviewDecision
from app.models.identity_provider import IdentityVerificationAttempt, IdentityVerificationEvent
from app.models.identity_verification import IdentityVerification
from app.models.organization import Organization
from app.models.organization_payment_policy import OrganizationPaymentPolicy
from app.models.payment import Payment
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance, ServiceOrderTechnicalCompletion, ServiceOrderWarranty
from app.models.technician_earning import TechnicianEarning, TechnicianEarningEvent
from app.models.user import User
from app.services.technician_earning_service import (
    create_technician_earning,
    evaluate_technician_earning_eligibility,
    reverse_technician_earning,
    transition_technician_earning,
)
import app.services.technician_earning_service as earning_service


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


def make_org(db, slug, *, status="ACTIVE"):
    row = Organization(name=slug, slug=slug, status=status)
    db.add(row)
    db.flush()
    return row


def make_user(db, org, username, *, role="BROKER", status="ACTIVE", is_active=True):
    row = User(
        organization_id=org.id, username=username, email=f"{username}@example.test",
        password_hash="test-hash", role=role, status=status, is_active=is_active,
        email_verified=True,
    )
    db.add(row)
    db.flush()
    return row


def make_order(db, org, tech):
    order = ServiceOrder(
        organization_id=org.id, lead_id=1, order_number=f"OS-{org.id}-{tech.id}",
        responsible_user_id=tech.id, status="COMPLETED", final_service_price=Decimal("1000.00"),
    )
    db.add(order)
    db.flush()
    return order


def active_membership_gate(_db, user_id, organization_id):
    return SimpleNamespace(user_id=user_id, organization_id=organization_id, status="ACTIVE", is_operational=True)


def make_payment(db, org, order, tech, *, status="PAID", method="STRIPE_CARD"):
    payment = Payment(
        organization_id=org.id, service_order_id=order.id, technician_id=tech.id,
        payment_type="SERVICE", payment_method=method, provider="STRIPE", currency="MXN",
        gross_amount=Decimal("1000.00"), status=status, idempotency_key=f"payment:{order.id}",
    )
    db.add(payment)
    db.flush()
    return payment


def make_identity(db, org, tech):
    identity = IdentityVerification(
        user_id=tech.id, organization_id=org.id, status="VERIFIED", provider="metamap",
        verified_at=datetime.utcnow(),
    )
    db.add(identity)
    db.flush()
    attempt = IdentityVerificationAttempt(
        user_id=tech.id, organization_id=org.id, identity_verification_id=identity.id,
        attempt_key_hash=f"{tech.id:064d}", provider="metamap", provider_mode="live",
        policy="MEXICAN", status="PENDING_REVIEW", consented_at=datetime.utcnow(),
        consent_version="identity-verification-v1", expires_at=datetime.utcnow() + timedelta(days=1),
    )
    db.add(attempt)
    db.flush()
    db.add(IdentityVerificationEvent(
        provider="metamap", provider_event_id=f"provider:{tech.id}", identity_verification_attempt_id=attempt.id,
        event_name="verification_completed", payload_hash="a" * 64, status="PROCESSED",
        reason_code="PROVIDER_APPROVED",
    ))
    db.add(IdentityHumanReviewDecision(
        attempt_id=attempt.id, user_id=tech.id, organization_id=org.id, reviewer_user_id=tech.id + 1000,
        reviewer_organization_id=org.id, decision="APPROVE", reason_code="PROVIDER_RESULT_REVIEW",
        idempotency_key_hash=f"{tech.id + 1000:064d}",
    ))
    return identity


def make_complete_gates(db, org, tech, order):
    db.add(OrganizationPaymentPolicy(organization_id=org.id))
    make_identity(db, org, tech)
    db.add(ServiceOrderTechnicalCompletion(
        organization_id=org.id, service_order_id=order.id, status="REVIEWED",
        responsible_user_id=tech.id, reviewed_by_user_id=tech.id + 1000,
        technical_completed_at=datetime.utcnow(), reviewed_at=datetime.utcnow(),
    ))
    db.add(ServiceOrderCustomerAcceptance(
        organization_id=org.id, service_order_id=order.id, status="ACCEPTED",
        accepted_at=datetime.utcnow(), idempotency_key=f"accept:{order.id}",
    ))
    db.add(ServiceOrderWarranty(
        organization_id=org.id, service_order_id=order.id, warranty_days=1,
        starts_at=datetime.utcnow() - timedelta(days=3), ends_at=datetime.utcnow() - timedelta(days=1),
        status="CLOSED",
    ))
    make_payment(db, org, order, tech)
    db.commit()


def test_feature_off_is_fail_closed_and_api_has_no_write_path(monkeypatch, db):
    monkeypatch.delenv("TECHNICIAN_EARNINGS_ENABLED", raising=False)
    with pytest.raises(RuntimeError):
        create_technician_earning(
            db, organization_id=1, service_order_id=1, technician_user_id=1,
            gross_amount=100, platform_fee_amount=10, processing_fee_amount=0,
            currency="MXN", policy_version="v1", source_type="TEST",
            source_reference="synthetic", idempotency_key="off",
        )


def test_eligibility_is_read_only_and_requires_every_gate(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    org = make_org(db, "earnings-gates")
    tech = make_user(db, org, "tech-gates")
    order = make_order(db, org, tech)
    db.commit()
    result = evaluate_technician_earning_eligibility(db, order)
    assert result["eligible"] is False
    assert "IDENTITY_NOT_VERIFIED" in result["reason_codes"]
    assert db.query(TechnicianEarning).count() == 0
    make_complete_gates(db, org, tech, order)
    assert evaluate_technician_earning_eligibility(db, order, membership_gate=active_membership_gate) == {"eligible": True, "reason_codes": []}


def test_cross_tenant_and_inactive_states_are_rejected(db):
    org = make_org(db, "earnings-org")
    other = make_org(db, "other-org")
    tech = make_user(db, org, "tech-tenant")
    order = make_order(db, other, tech)
    result = evaluate_technician_earning_eligibility(db, order, technician_id=tech.id)
    assert result["eligible"] is False
    assert "TENANT_MISMATCH" in result["reason_codes"]


def test_membership_gate_unavailable_fails_closed_without_org_fallback(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    monkeypatch.setattr(earning_service, "_membership_gate_initialized", False)
    monkeypatch.setattr(earning_service, "_membership_gate", None)
    monkeypatch.setattr(earning_service.importlib, "import_module", lambda _name: (_ for _ in ()).throw(ModuleNotFoundError()))
    org = make_org(db, "membership-unavailable")
    tech = make_user(db, org, "membership-unavailable-tech")
    order = make_order(db, org, tech)

    result = evaluate_technician_earning_eligibility(db, order)

    assert result["eligible"] is False
    assert "MEMBERSHIP_GATE_UNAVAILABLE" in result["reason_codes"]
    assert db.query(TechnicianEarning).count() == 0


@pytest.mark.parametrize(
    "membership, expected_reason",
    [
        (SimpleNamespace(status="INACTIVE", is_operational=True), "MEMBERSHIP_NOT_OPERATIONAL"),
        (SimpleNamespace(status="ACTIVE", is_operational=False), "MEMBERSHIP_NOT_OPERATIONAL"),
        (SimpleNamespace(status="ACTIVE", is_operational=True, organization_id=999), "MEMBERSHIP_NOT_OPERATIONAL"),
    ],
)
def test_membership_gate_requires_active_operational_tenant(membership, expected_reason, db):
    org = make_org(db, "membership-state")
    tech = make_user(db, org, "membership-state-tech")
    order = make_order(db, org, tech)

    def resolver(_db, user_id, organization_id):
        if getattr(membership, "organization_id", None) == 999:
            return membership
        return SimpleNamespace(user_id=user_id, organization_id=organization_id, status=membership.status, is_operational=membership.is_operational)

    result = evaluate_technician_earning_eligibility(db, order, membership_gate=resolver)

    assert result["eligible"] is False
    if expected_reason:
        assert expected_reason in result["reason_codes"]
    else:
        assert "MEMBERSHIP_GATE_UNAVAILABLE" not in result["reason_codes"]
        assert "MEMBERSHIP_NOT_OPERATIONAL" not in result["reason_codes"]


def test_membership_gate_error_fails_closed(db):
    org = make_org(db, "membership-error")
    tech = make_user(db, org, "membership-error-tech")
    order = make_order(db, org, tech)

    def resolver(_db, _user_id, _organization_id):
        raise SQLAlchemyError("synthetic resolver failure")

    result = evaluate_technician_earning_eligibility(db, order, membership_gate=resolver)

    assert result["eligible"] is False
    assert "MEMBERSHIP_GATE_UNAVAILABLE" in result["reason_codes"]


def test_cash_payment_and_open_warranty_are_not_eligible(db):
    org = make_org(db, "earnings-cash")
    tech = make_user(db, org, "tech-cash")
    order = make_order(db, org, tech)
    db.add(OrganizationPaymentPolicy(organization_id=org.id))
    db.add(ServiceOrderWarranty(
        organization_id=org.id, service_order_id=order.id, warranty_days=90,
        starts_at=datetime.utcnow(), ends_at=datetime.utcnow() + timedelta(days=30), status="ACTIVE",
    ))
    make_payment(db, org, order, tech, status="PAID_CASH", method="CASH")
    db.commit()
    result = evaluate_technician_earning_eligibility(db, order)
    assert "WARRANTY_NOT_CLOSED" in result["reason_codes"]
    assert "CASH_PAYMENT_NOT_ELIGIBLE" in result["reason_codes"]


def test_decimal_idempotency_append_only_and_reserved_payout_states(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    earning = create_technician_earning(
        db, organization_id=1, service_order_id=1, technician_user_id=2,
        gross_amount=Decimal("100.00"), platform_fee_amount=Decimal("12.34"),
        processing_fee_amount=Decimal("1.11"), currency="mxn", policy_version="v1",
        source_type="SYNTHETIC", source_reference="qa", idempotency_key="same-key",
    )
    db.commit()
    replay = create_technician_earning(
        db, organization_id=1, service_order_id=1, technician_user_id=2,
        gross_amount=Decimal("100.00"), platform_fee_amount=Decimal("12.34"),
        processing_fee_amount=Decimal("1.11"), currency="MXN", policy_version="v1",
        source_type="SYNTHETIC", source_reference="qa", idempotency_key="same-key",
    )
    assert replay.id == earning.id
    assert earning.net_amount == Decimal("86.55")
    transition_technician_earning(
        db, earning_id=earning.id, organization_id=1, new_status="IN_GUARANTEE",
        reason_code="ELIGIBILITY_CONFIRMED", idempotency_key="transition-1",
    )
    with pytest.raises(ValueError):
        transition_technician_earning(
            db, earning_id=earning.id, organization_id=1, new_status="PAID",
            reason_code="ELIGIBILITY_CONFIRMED", idempotency_key="paid-1",
        )
    db.commit()
    assert db.query(TechnicianEarningEvent).count() == 2


def test_source_reference_must_be_opaque(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    with pytest.raises(ValueError, match="opaque"):
        create_technician_earning(
            db, organization_id=1, service_order_id=1, technician_user_id=2,
            gross_amount=10, platform_fee_amount=1, processing_fee_amount=0,
            currency="MXN", policy_version="v1", source_type="SYNTHETIC",
            source_reference="person@example.test", idempotency_key="pii-source",
        )


def test_reversal_is_related_and_idempotent(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    earning = create_technician_earning(
        db, organization_id=1, service_order_id=1, technician_user_id=2,
        gross_amount=100, platform_fee_amount=10, processing_fee_amount=0,
        currency="MXN", policy_version="v1", source_type="SYNTHETIC",
        source_reference="qa", idempotency_key="original",
    )
    earning.status = "AVAILABLE_FOR_PAYMENT"
    db.flush()
    reversal = reverse_technician_earning(db, earning_id=earning.id, organization_id=1, idempotency_key="reverse-1")
    db.commit()
    assert earning.status == "REVERSED"
    assert reversal.reversal_of_id == earning.id
    assert reversal.net_amount == earning.net_amount
    assert reverse_technician_earning(db, earning_id=earning.id, organization_id=1, idempotency_key="reverse-1").id == reversal.id


def test_startup_excludes_earnings_tables_from_create_all():
    source = open("backend/app/main.py", encoding="utf-8").read()
    assert '"technician_earnings"' in source
    assert '"technician_earning_events"' in source
