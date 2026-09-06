from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.service_order_ledger_entry import ServiceOrderLedgerEntry
from app.models.visit_pricing_snapshot import VisitPricingSnapshot
from app.models.organization_payment_policy import OrganizationPaymentPolicy
from app.models.lead import Lead
from app.models.payment import Payment
from app.models.service_order_tracking import ServiceOrderTracking
from app.models.service_property import ServiceProperty
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.organization_marketplace_link import OrganizationMarketplaceLink
from app.services.service_order_financial_service import (
    append_ledger_entry,
    calculate_marketplace_fee,
    calculate_order_balance,
    calculate_provider_earning,
    can_dispatch_service_order,
    create_visit_pricing_snapshot,
    ensure_financial_account,
    get_financial_snapshot,
    payment_schedule,
    resolve_payment_policy,
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


def order(db, organization_id):
    item = ServiceOrder(organization_id=organization_id, lead_id=1, status="ABERTA")
    db.add(item)
    db.flush()
    return item


def orgs(db):
    first = Organization(name="One", slug="one")
    second = Organization(name="Two", slug="two")
    db.add_all([first, second])
    db.flush()
    return first, second


def test_financial_account_is_one_to_one_and_idempotent(db):
    first, _ = orgs(db)
    item = order(db, first.id)
    account = ensure_financial_account(db, item, organization_id=first.id)
    same = ensure_financial_account(db, item, organization_id=first.id, order_origin="MARKETPLACE")
    assert account.id == same.id
    assert same.order_origin == "PRIVATE"
    assert db.query(ServiceOrderFinancial).count() == 1


def test_financial_entities_reject_cross_tenant_order(db):
    first, second = orgs(db)
    item = order(db, first.id)
    with pytest.raises(ValueError, match="does not belong"):
        ensure_financial_account(db, item, organization_id=second.id)
    with pytest.raises(ValueError, match="does not belong"):
        create_visit_pricing_snapshot(db, item, organization_id=second.id, pricing={"total_amount": 1})
    with pytest.raises(ValueError, match="does not belong"):
        append_ledger_entry(db, item, organization_id=second.id, entry_type="VISIT_CHARGE", amount=1, idempotency_key="x")


def test_policy_defaults_are_organization_scoped(db):
    first, second = orgs(db)
    policy = resolve_payment_policy(db, organization_id=first.id)
    assert policy.visit_required is True
    assert policy.visit_payment_timing == "PREPAID"
    assert policy.small_service_limit == Decimal("3000.000")
    assert policy.medium_service_limit == Decimal("15000.000")
    assert policy.marketplace_fee_rate == Decimal("10.000")
    assert resolve_payment_policy(db, organization_id=first.id).organization_id == first.id
    assert resolve_payment_policy(db, organization_id=second.id).organization_id == second.id
    assert db.query(OrganizationPaymentPolicy).count() == 2


def test_marketplace_fee_private_is_zero_and_marketplace_uses_policy(db):
    first, _ = orgs(db)
    policy = resolve_payment_policy(db, organization_id=first.id)
    assert calculate_marketplace_fee(1000, order_origin="PRIVATE", policy=policy) == Decimal("0.00")
    assert calculate_marketplace_fee(1000, order_origin="MARKETPLACE", policy=policy) == Decimal("100.00")
    assert calculate_marketplace_fee(1000, order_origin="MARKETPLACE_ESCALATED", policy=policy) == Decimal("100.00")


def test_visit_pricing_snapshot_is_frozen(db):
    first, _ = orgs(db)
    item = order(db, first.id)
    pricing = {"base_price": 450, "zone_fee": 100, "total_amount": 550, "pricing_version": "CANCUN_V1"}
    snapshot = create_visit_pricing_snapshot(db, item, organization_id=first.id, pricing=pricing)
    assert snapshot.total_amount == Decimal("550.00")
    assert create_visit_pricing_snapshot(db, item, organization_id=first.id, pricing=pricing).id == snapshot.id
    with pytest.raises(ValueError, match="immutable"):
        create_visit_pricing_snapshot(db, item, organization_id=first.id, pricing={**pricing, "total_amount": 600})


def test_ledger_append_is_idempotent_and_history_is_not_overwritten(db):
    first, _ = orgs(db)
    item = order(db, first.id)
    charge = append_ledger_entry(db, item, organization_id=first.id, entry_type="VISIT_CHARGE", amount=1000, idempotency_key="visit-1")
    assert append_ledger_entry(db, item, organization_id=first.id, entry_type="VISIT_CHARGE", amount=999, idempotency_key="visit-1").id == charge.id
    append_ledger_entry(db, item, organization_id=first.id, entry_type="VISIT_PAYMENT", amount=300, idempotency_key="payment-1")
    append_ledger_entry(db, item, organization_id=first.id, entry_type="REFUND", amount=50, idempotency_key="refund-1")
    balance = calculate_order_balance(db, item, organization_id=first.id)
    assert balance["charges"] == Decimal("1000.00")
    assert balance["payments"] == Decimal("300.00")
    assert balance["refunds"] == Decimal("50.00")
    assert balance["outstanding_balance"] == Decimal("650.00")
    assert db.query(ServiceOrderLedgerEntry).count() == 3


def test_financial_snapshot_reads_account_and_ledger_balance(db):
    first, _ = orgs(db)
    item = order(db, first.id)
    account = ensure_financial_account(db, item, organization_id=first.id)
    append_ledger_entry(db, item, organization_id=first.id, entry_type="VISIT_CHARGE", amount=700, idempotency_key="snapshot-charge")
    snapshot = get_financial_snapshot(db, item, organization_id=first.id)
    assert snapshot["account"].id == account.id
    assert snapshot["financial_status"] == "NO_CHARGE"
    assert snapshot["balance"]["outstanding_balance"] == Decimal("700.00")


def test_provider_earning_and_processing_fee_do_not_change_customer_balance(db):
    first, _ = orgs(db)
    item = order(db, first.id)
    append_ledger_entry(db, item, organization_id=first.id, entry_type="VISIT_CHARGE", amount=1000, idempotency_key="charge")
    append_ledger_entry(db, item, organization_id=first.id, entry_type="PROCESSING_FEE", amount=30, idempotency_key="processor")
    assert calculate_order_balance(db, item, organization_id=first.id)["outstanding_balance"] == Decimal("1000.00")
    assert calculate_provider_earning(1000, 100, 30) == Decimal("870.00")


def test_dispatch_policy_and_payment_thresholds(db):
    first, _ = orgs(db)
    item = order(db, first.id)
    account = ensure_financial_account(db, item, organization_id=first.id, order_origin="MARKETPLACE")
    policy = resolve_payment_policy(db, organization_id=first.id)
    empty = calculate_order_balance(db, item, organization_id=first.id)
    assert can_dispatch_service_order(item, financial_account=account, policy=policy, balance=empty) is False
    append_ledger_entry(db, item, organization_id=first.id, entry_type="VISIT_CHARGE", amount=450, idempotency_key="charge")
    append_ledger_entry(db, item, organization_id=first.id, entry_type="VISIT_PAYMENT", amount=450, idempotency_key="paid")
    account.financial_status = "VISIT_PAID"
    assert can_dispatch_service_order(item, financial_account=account, policy=policy,
                                      balance=calculate_order_balance(db, item, organization_id=first.id)) is True
    policy.visit_payment_timing = "ON_ARRIVAL"
    account.financial_status = "NO_CHARGE"
    assert can_dispatch_service_order(item, financial_account=account, policy=policy, balance=empty) is True


def test_payment_schedule_uses_configured_small_medium_and_large_thresholds(db):
    first, _ = orgs(db)
    policy = resolve_payment_policy(db, organization_id=first.id)
    assert [(stage["stage"], stage["percentage"]) for stage in payment_schedule(3000, policy=policy)] == [("FULL", Decimal("100"))]
    medium = payment_schedule(10000, policy=policy)
    assert [stage["percentage"] for stage in medium] == [Decimal("30"), Decimal("70")]
    assert sum(stage["amount"] for stage in medium) == Decimal("10000.00")
    large = payment_schedule(20000, policy=policy)
    assert [stage["percentage"] for stage in large] == [Decimal("30"), Decimal("40"), Decimal("30")]
    assert sum(stage["amount"] for stage in large) == Decimal("20000.00")


def test_new_financial_models_are_registered_in_metadata():
    assert {
        "service_order_financials", "service_order_ledger_entries",
        "visit_pricing_snapshots", "organization_payment_policies",
    }.issubset(Base.metadata.tables)
