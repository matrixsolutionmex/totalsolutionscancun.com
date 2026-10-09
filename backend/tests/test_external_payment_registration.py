from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  Ensures all manual-migration models are registered.
from app.database.connection import Base
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.organization_payment_policy import OrganizationPaymentPolicy
from app.models.service_order import ServiceOrder
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.models.visit_pricing_snapshot import VisitPricingSnapshot
from app.services.payment_service import record_external_payment, stripe_expected_livemode


@pytest.fixture()
def external_payment_db(monkeypatch):
    monkeypatch.delenv("STRIPE_EXPECTED_LIVEMODE", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    organization = Organization(name="External QA", slug="external-qa")
    db.add(organization)
    db.flush()
    actor = User(
        organization_id=organization.id, username="root-external-qa", password_hash="x",
        role="ROOT", status="ACTIVE", email_verified=True,
    )
    lead = Lead(organization_id=organization.id, nome="Synthetic customer", tipo_servico="PLUMBING")
    db.add_all([actor, lead])
    db.flush()
    request = ServiceRequest(
        organization_id=organization.id, lead_id=lead.id, tracking_token="external-qa-token",
        service_category="PLUMBING", requester_name="Synthetic customer",
    )
    db.add(request)
    db.flush()
    order = ServiceOrder(
        organization_id=organization.id, lead_id=lead.id, service_request_id=request.id,
        order_number="TS-QA-EXTERNAL", status="ABERTA",
    )
    db.add(order)
    db.flush()
    db.add_all([
        ServiceOrderFinancial(
            organization_id=organization.id, service_order_id=order.id,
            financial_status="VISIT_PAYMENT_PENDING", amount_due=Decimal("450.00"),
        ),
        VisitPricingSnapshot(
            organization_id=organization.id, service_order_id=order.id,
            total_amount=Decimal("450.00"), currency="MXN", pricing_version="test",
        ),
        OrganizationPaymentPolicy(organization_id=organization.id),
    ])
    db.commit()
    try:
        yield db, actor, order
    finally:
        db.close()


def test_external_visit_is_audited_and_idempotent(external_payment_db):
    db, actor, order = external_payment_db
    payment = record_external_payment(
        db, order=order, actor=actor, amount=Decimal("450.00"), currency="MXN",
        purpose="VISIT", payment_method="BANK_TRANSFER", external_reference="QA-EXT-1",
        evidence_reference="synthetic-proof", observation="synthetic confirmation",
    )
    db.commit()
    repeated = record_external_payment(
        db, order=order, actor=actor, amount=Decimal("450.00"), currency="MXN",
        purpose="VISIT", payment_method="BANK_TRANSFER", external_reference="QA-EXT-1",
    )
    assert repeated.id == payment.id
    assert payment.status == "PAID"
    assert db.query(ServiceOrderFinancial).one().financial_status == "VISIT_PAID"


def test_external_visit_rejects_amount_mismatch_without_new_payment(external_payment_db):
    db, actor, order = external_payment_db
    with pytest.raises(ValueError, match="snapshot"):
        record_external_payment(
            db, order=order, actor=actor, amount=Decimal("449.00"), currency="MXN",
            purpose="VISIT", payment_method="CASH", external_reference="QA-EXT-2",
        )
    db.rollback()
    assert db.query(ServiceOrderFinancial).one().financial_status == "VISIT_PAYMENT_PENDING"


def test_production_without_explicit_stripe_mode_defaults_live(monkeypatch):
    monkeypatch.delenv("STRIPE_EXPECTED_LIVEMODE", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert stripe_expected_livemode() is True
