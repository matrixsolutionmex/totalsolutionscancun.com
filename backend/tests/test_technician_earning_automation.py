from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401
from app.database.connection import Base
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance, ServiceOrderWarranty
from app.models.technician_compensation import ServiceOrderCompensationSnapshot
from app.models.technician_earning import TechnicianEarning
from app.models.user import User
import app.services.technician_earning_reconciliation_service as automation_service
from app.services.technician_earning_service import earning_payload


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
from app.services.technician_earning_reconciliation_service import reconcile_confirmed_payment


def _fixture(db, org, tech, *, amount="100.00"):
    order = ServiceOrder(
        organization_id=org.id, lead_id=1, order_number=f"AUTO-{org.id}-{tech.id}",
        responsible_user_id=tech.id, status="ABERTA",
    )
    db.add(order)
    db.flush()
    snapshot = ServiceOrderCompensationSnapshot(
        organization_id=org.id, service_order_id=order.id, technician_user_id=tech.id,
        policy_id=1, policy_version=1, currency="MXN", labor_base_amount=Decimal(amount),
        material_amount=Decimal("40.00"), tax_amount=Decimal("20.00"), reimbursement_amount=Decimal("10.00"),
        discount_amount=Decimal("0.00"), technician_share_bps=7500, organization_share_bps=2500,
        technician_amount=Decimal("75.00"), organization_amount=Decimal("25.00"), guarantee_days=7,
        installment_rule="PROPORTIONAL_CONFIRMED_INSTALLMENTS", status="FROZEN",
        idempotency_key_hash=("a" * 63) + str(order.id % 10),
    )
    db.add(snapshot)
    db.flush()
    return order


def _payment(db, org, order, tech, key, amount):
    payment = Payment(
        organization_id=org.id, service_order_id=order.id, technician_id=tech.id,
        payment_type="SERVICE_STAGE_1", payment_method="STRIPE_CARD", currency="MXN",
        gross_amount=Decimal(amount), status="PAID", provider="STRIPE", idempotency_key=key,
    )
    db.add(payment)
    db.flush()
    return payment


def test_confirmed_installments_recognize_only_incremental_capped_amount(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNING_AUTOMATION_ENABLED", "true")
    org = Organization(name="automation-org", slug="automation-org", status="ACTIVE")
    db.add(org)
    db.flush()
    tech = User(
        organization_id=org.id, username="automation-tech", email="automation-tech@example.test",
        password_hash="test-hash", role="BROKER", status="ACTIVE", is_active=True, email_verified=True,
    )
    db.add(tech)
    db.flush()
    monkeypatch.setattr(automation_service, "_membership_gate_result", lambda *_args, **_kwargs: None)
    order = _fixture(db, org, tech)
    first = _payment(db, org, order, tech, "automation-payment-1", "50.00")
    second = _payment(db, org, order, tech, "automation-payment-2", "50.00")
    db.commit()

    first_result = reconcile_confirmed_payment(db, first, provider_event_key="provider-event-1")
    db.flush()
    assert first_result["recognized"] is True
    assert Decimal(first_result["delta"]) == Decimal("37.50")

    second_result = reconcile_confirmed_payment(db, second, provider_event_key="provider-event-2")
    db.flush()
    assert Decimal(second_result["delta"]) == Decimal("37.50")
    duplicate = reconcile_confirmed_payment(db, second, provider_event_key="provider-event-2")
    assert duplicate.get("reason_code") == "DUPLICATE_PROVIDER_EVENT", duplicate
    assert sum((row.net_amount for row in db.query(TechnicianEarning).filter_by(service_order_id=order.id)), Decimal("0.00")) == Decimal("75.00")


def test_automation_is_off_by_default_and_requires_frozen_snapshot(monkeypatch, db):
    org = Organization(name="automation-gate-org", slug="automation-gate-org", status="ACTIVE")
    db.add(org)
    db.flush()
    tech = User(
        organization_id=org.id, username="automation-gate-tech", email="automation-gate-tech@example.test",
        password_hash="test-hash", role="BROKER", status="ACTIVE", is_active=True, email_verified=True,
    )
    db.add(tech)
    db.flush()
    order = ServiceOrder(
        organization_id=org.id, lead_id=1, order_number="AUTO-GATE", responsible_user_id=tech.id,
        status="ABERTA",
    )
    db.add(order)
    db.flush()
    payment = _payment(db, org, order, tech, "automation-gate-payment", "50.00")
    db.commit()

    monkeypatch.delenv("TECHNICIAN_EARNING_AUTOMATION_ENABLED", raising=False)
    assert reconcile_confirmed_payment(db, payment, provider_event_key="gate-off")["reason_code"] == "AUTOMATION_DISABLED"
    monkeypatch.setenv("TECHNICIAN_EARNING_AUTOMATION_ENABLED", "true")
    assert reconcile_confirmed_payment(db, payment, provider_event_key="gate-no-snapshot")["reason_code"] == "FROZEN_SNAPSHOT_REQUIRED"
    assert db.query(TechnicianEarning).count() == 0


def test_refund_reversal_is_linked_to_the_positive_earning(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNING_AUTOMATION_ENABLED", "true")
    org = Organization(name="reversal-org", slug="reversal-org", status="ACTIVE")
    db.add(org)
    db.flush()
    tech = User(
        organization_id=org.id, username="reversal-tech", email="reversal-tech@example.test",
        password_hash="test-hash", role="BROKER", status="ACTIVE", is_active=True,
    )
    db.add(tech)
    db.flush()
    monkeypatch.setattr(automation_service, "_membership_gate_result", lambda *_args, **_kwargs: None)
    order = _fixture(db, org, tech)
    payment = _payment(db, org, order, tech, "reversal-payment", "50.00")
    db.commit()
    automation_service.reconcile_confirmed_payment(db, payment, provider_event_key="reversal-confirmed")
    result = automation_service.reconcile_payment_adjustment(
        db, payment, provider_event_key="reversal-refund", amount=Decimal("20.00"), event_type="REFUND",
    )
    db.commit()
    reversal = db.query(TechnicianEarning).filter_by(source_type="AUTOMATED_ADJUSTMENT").one()
    original = db.query(TechnicianEarning).filter_by(id=reversal.reversal_of_id).one()
    assert Decimal(result["delta"]) == Decimal("15.00")
    assert reversal.reversal_of_id == original.id
    assert original.source_type == "AUTOMATED_PAYMENT"
    assert reversal.net_amount == Decimal("15.00")


def test_due_release_requires_expired_warranty_and_timezone_aware_now(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_EARNING_AUTOMATION_ENABLED", "true")
    org = Organization(name="due-org", slug="due-org", status="ACTIVE")
    db.add(org)
    db.flush()
    tech = User(
        organization_id=org.id, username="due-tech", email="due-tech@example.test",
        password_hash="test-hash", role="BROKER", status="ACTIVE", is_active=True,
    )
    db.add(tech)
    db.flush()
    monkeypatch.setattr(automation_service, "_membership_gate_result", lambda *_args, **_kwargs: None)
    order = _fixture(db, org, tech)
    payment = _payment(db, org, order, tech, "due-payment", "50.00")
    accepted = ServiceOrderCustomerAcceptance(
        organization_id=org.id, service_order_id=order.id, status="ACCEPTED",
        accepted_at=datetime.utcnow(), idempotency_key="due-acceptance",
    )
    warranty = ServiceOrderWarranty(
        organization_id=org.id, service_order_id=order.id, warranty_days=7,
        starts_at=datetime.utcnow(), ends_at=datetime.utcnow() + timedelta(days=7), status="ACTIVE",
    )
    db.add_all([accepted, warranty])
    db.commit()
    automation_service.reconcile_confirmed_payment(db, payment, provider_event_key="due-confirmed")
    db.commit()
    assert automation_service.on_order_customer_accepted(db, order, event_key="due-start")["updated"] == 1
    db.commit()
    order.status = "COMPLETED"
    payment.status = "PAID"
    db.flush()
    with pytest.raises(ValueError, match="timezone-aware"):
        automation_service.release_due_technician_earnings(db, datetime.utcnow(), 10)
    early = automation_service.release_due_technician_earnings(db, datetime.now(timezone.utc), 10)
    assert early["updated"] == 0
    warranty.ends_at = datetime.utcnow() - timedelta(seconds=1)
    due = automation_service.release_due_technician_earnings(db, datetime.now(timezone.utc), 10)
    assert due["updated"] == 1
    db.commit()
    assert db.query(TechnicianEarning).one().status == "AVAILABLE_FOR_PAYMENT"


def test_technician_statement_payload_excludes_internal_fees():
    earning = TechnicianEarning(
        id=1, service_order_id=2, currency="MXN", gross_amount=100,
        platform_fee_amount=25, processing_fee_amount=3, net_amount=72,
        policy_version="1", source_type="AUTOMATED_PAYMENT", status="PROCESSING",
        organization_id=1, technician_user_id=2, source_reference="qa", idempotency_key_hash="x" * 64,
    )
    payload = earning_payload(earning)
    assert Decimal(payload["net_amount"]) == Decimal("72.00")
    assert "platform_fee_amount" not in payload
    assert "processing_fee_amount" not in payload
