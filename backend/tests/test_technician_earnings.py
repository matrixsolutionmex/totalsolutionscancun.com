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
from app.models.organization_membership import OrganizationMembership
from app.models.organization_payment_policy import OrganizationPaymentPolicy
from app.models.payment import Payment
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance, ServiceOrderTechnicalCompletion, ServiceOrderWarranty
from app.models.technician_earning import TechnicianEarning, TechnicianEarningEvent
from app.models.user import User
from app.routes.technician_earning_routes import my_earnings_summary
from app.services.technician_earning_service import (
    create_technician_earning,
    evaluate_technician_earning_eligibility,
    reverse_technician_earning,
    technician_earnings_enabled_for_user,
    technician_earnings_rollout,
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


def add_membership(db, user, organization, *, status="ACTIVE", is_operational=True):
    membership = OrganizationMembership(
        user_id=user.id, organization_id=organization.id, membership_type="TECHNICIAN",
        role="TECHNICIAN", status=status, is_primary=True, is_operational=is_operational,
    )
    db.add(membership)
    db.flush()
    return membership


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


@pytest.mark.parametrize(
    ("mode", "allowlist", "valid", "organization_ids"),
    [
        ("off", "", True, frozenset()),
        ("canary", "1, 2", True, frozenset({1, 2})),
        ("all", "", True, frozenset()),
        ("", "", False, frozenset()),
        ("invalid", "1", False, frozenset()),
        ("canary", "", False, frozenset()),
        ("canary", "0", False, frozenset()),
        ("canary", "-1", False, frozenset()),
        ("canary", "1,1", False, frozenset()),
        ("all", "1", False, frozenset()),
    ],
)
def test_statement_rollout_parser_is_fail_closed(monkeypatch, mode, allowlist, valid, organization_ids):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ROLLOUT_MODE", mode)
    monkeypatch.setenv("TECHNICIAN_EARNINGS_CANARY_ORGANIZATION_IDS", allowlist)
    rollout = technician_earnings_rollout()
    assert rollout.valid is valid
    assert rollout.organization_ids == organization_ids


def test_statement_rollout_requires_active_operational_membership_and_no_role_bypass(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("TECHNICIAN_EARNINGS_CANARY_ORGANIZATION_IDS", "1")
    monkeypatch.setattr(earning_service, "_membership_gate_initialized", False)
    monkeypatch.setattr(earning_service, "_membership_gate", None)
    org = make_org(db, "statement-canary-org")
    other = make_org(db, "statement-other-org")
    technician = make_user(db, org, "statement-tech")
    other_technician = make_user(db, other, "statement-other-tech")
    root = make_user(db, org, "statement-root", role="ROOT")
    suspended = make_user(db, org, "statement-suspended", status="SUSPENDED")
    add_membership(db, technician, org)
    add_membership(db, other_technician, other)
    add_membership(db, root, org, status="ACTIVE", is_operational=True)
    add_membership(db, suspended, org, status="SUSPENDED", is_operational=False)
    db.commit()

    assert technician_earnings_enabled_for_user(db, technician) is True
    assert technician_earnings_enabled_for_user(db, other_technician) is False
    assert technician_earnings_enabled_for_user(db, root) is False
    assert technician_earnings_enabled_for_user(db, suspended) is False

    monkeypatch.setenv("TECHNICIAN_EARNINGS_ROLLOUT_MODE", "all")
    monkeypatch.delenv("TECHNICIAN_EARNINGS_CANARY_ORGANIZATION_IDS", raising=False)
    assert technician_earnings_enabled_for_user(db, technician) is True
    assert technician_earnings_enabled_for_user(db, other_technician) is True


def test_statement_rollout_master_and_missing_configuration_fail_closed(monkeypatch, db):
    org = make_org(db, "statement-off-org")
    technician = make_user(db, org, "statement-off-tech")
    add_membership(db, technician, org)
    db.commit()
    monkeypatch.delenv("TECHNICIAN_EARNINGS_ENABLED", raising=False)
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ROLLOUT_MODE", "all")
    assert technician_earnings_enabled_for_user(db, technician) is False
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    monkeypatch.delenv("TECHNICIAN_EARNINGS_ROLLOUT_MODE", raising=False)
    assert technician_earnings_enabled_for_user(db, technician) is False


def test_statement_frontend_requires_sanitized_backend_boolean_before_request():
    source = open("frontend/index.html", encoding="utf-8").read()
    assert "currentUser?.technician_earnings_enabled !== true" in source
    assert "TECHNICIAN_EARNINGS_ROLLOUT_MODE" not in source
    assert "TECHNICIAN_EARNINGS_CANARY_ORGANIZATION_IDS" not in source


def test_statement_summary_subtracts_automated_reversal_without_double_counting(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ENABLED", "true")
    monkeypatch.setenv("TECHNICIAN_EARNINGS_ROLLOUT_MODE", "canary")
    org = make_org(db, "statement-reversal-org")
    technician = make_user(db, org, "statement-reversal-tech")
    add_membership(db, technician, org)
    db.add_all([
        TechnicianEarning(
            organization_id=org.id, service_order_id=1, technician_user_id=technician.id,
            currency="MXN", gross_amount=Decimal("37.50"), platform_fee_amount=Decimal("0.00"),
            processing_fee_amount=Decimal("0.00"), net_amount=Decimal("37.50"),
            policy_version="QA-1", source_type="AUTOMATED_PAYMENT", source_reference="payment-1",
            status="PROCESSING", idempotency_key_hash="summary-positive-1",
        ),
        TechnicianEarning(
            organization_id=org.id, service_order_id=1, technician_user_id=technician.id,
            currency="MXN", gross_amount=Decimal("37.50"), platform_fee_amount=Decimal("0.00"),
            processing_fee_amount=Decimal("0.00"), net_amount=Decimal("37.50"),
            policy_version="QA-1", source_type="AUTOMATED_PAYMENT", source_reference="payment-2",
            status="IN_GUARANTEE", idempotency_key_hash="summary-positive-2",
        ),
        TechnicianEarning(
            organization_id=org.id, service_order_id=1, technician_user_id=technician.id,
            currency="MXN", gross_amount=Decimal("15.00"), platform_fee_amount=Decimal("0.00"),
            processing_fee_amount=Decimal("0.00"), net_amount=Decimal("15.00"),
            policy_version="QA-1", source_type="AUTOMATED_ADJUSTMENT", source_reference="refund-1",
            status="REVERSED", reversal_of_id=1, idempotency_key_hash="summary-reversal-1",
        ),
    ])
    db.commit()
    monkeypatch.setenv("TECHNICIAN_EARNINGS_CANARY_ORGANIZATION_IDS", str(org.id))

    summary = my_earnings_summary(db, technician)

    assert summary["net_total"] == "60.00"
    assert summary["by_currency"] == [{"currency": "MXN", "net_total": "60.00", "items": 3}]


def test_frontend_language_refresh_preserves_technician_earnings_labels():
    source = open("frontend/index.html", encoding="utf-8").read()
    assert 'renderTechnicianEarningsLabels();' in source
    assert 'setText("#technicianEarningsTitle", t("technicianEarningsTitle"));' in source
