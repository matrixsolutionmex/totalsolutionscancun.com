"""Domain foundation for service-order finance; no checkout or dispatch side effects."""

import json
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from app.models.organization_payment_policy import OrganizationPaymentPolicy
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.service_order_ledger_entry import ServiceOrderLedgerEntry
from app.models.visit_pricing_snapshot import VisitPricingSnapshot


FINANCIAL_STATUSES = frozenset({
    "NO_CHARGE", "VISIT_PAYMENT_PENDING", "VISIT_PAID", "QUOTE_PAYMENT_PENDING",
    "PARTIALLY_PAID", "PAID", "BALANCE_DUE", "REFUND_PENDING", "PARTIALLY_REFUNDED",
    "REFUNDED", "DISPUTED",
})
ORDER_ORIGINS = frozenset({"PRIVATE", "MARKETPLACE", "MARKETPLACE_ESCALATED"})
CHARGE_TYPES = frozenset({"VISIT_CHARGE", "DEPOSIT"})
PAYMENT_TYPES = frozenset({"VISIT_PAYMENT", "SERVICE_PAYMENT", "CASH_PAYMENT", "BANK_TRANSFER", "DISCOUNT"})
PRICING_STATUSES = frozenset({"UNKNOWN", "CONFIGURED", "UNAVAILABLE", "WAIVED"})


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _require_order_scope(order, organization_id: int):
    if not order or order.organization_id != organization_id:
        raise ValueError("service order does not belong to organization")


def is_pricing_unavailable(order, financial_account) -> bool:
    if getattr(financial_account, "pricing_status", "UNKNOWN") == "UNAVAILABLE":
        return True
    if getattr(financial_account, "pricing_status", "UNKNOWN") != "UNKNOWN":
        return False
    try:
        snapshot = json.loads(getattr(order, "pricing_snapshot_json", None) or "{}")
    except (TypeError, ValueError):
        return False
    return bool(snapshot.get("unavailable_reason"))


def ensure_financial_account(db: Session, order, *, organization_id: int, order_origin: str | None = None):
    _require_order_scope(order, organization_id)
    origin = (order_origin or "PRIVATE").upper()
    if origin not in ORDER_ORIGINS:
        raise ValueError("invalid order origin")
    account = db.query(ServiceOrderFinancial).filter_by(service_order_id=order.id).first()
    if account:
        if account.organization_id != organization_id:
            raise ValueError("financial account tenant mismatch")
        return account
    account = ServiceOrderFinancial(
        organization_id=organization_id, service_order_id=order.id, order_origin=origin,
        currency="MXN", financial_status="NO_CHARGE", pricing_status="UNKNOWN",
    )
    db.add(account)
    db.flush()
    return account


def resolve_payment_policy(db: Session, *, organization_id: int) -> OrganizationPaymentPolicy:
    policy = db.query(OrganizationPaymentPolicy).filter_by(organization_id=organization_id).first()
    if policy:
        return policy
    policy = OrganizationPaymentPolicy(organization_id=organization_id)
    db.add(policy)
    db.flush()
    return policy


def create_visit_pricing_snapshot(db: Session, order, *, organization_id: int, pricing: dict):
    _require_order_scope(order, organization_id)
    existing = db.query(VisitPricingSnapshot).filter_by(service_order_id=order.id).first()
    values = {key: _money(pricing.get(key)) for key in (
        "base_price", "zone_fee", "distance_fee", "urgency_fee", "after_hours_fee",
        "discount_amount", "tax_amount", "total_amount",
    )}
    if existing:
        if existing.organization_id != organization_id or any(getattr(existing, key) != value for key, value in values.items()):
            raise ValueError("visit pricing snapshot is immutable")
        return existing
    snapshot = VisitPricingSnapshot(
        organization_id=organization_id, service_order_id=order.id, currency=str(pricing.get("currency") or "MXN").upper(),
        pricing_version=str(pricing.get("pricing_version") or "UNKNOWN"), **values,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def append_ledger_entry(db: Session, order, *, organization_id: int, entry_type: str, amount, idempotency_key: str, **kwargs):
    _require_order_scope(order, organization_id)
    existing = db.query(ServiceOrderLedgerEntry).filter_by(idempotency_key=idempotency_key).first()
    if existing:
        if existing.organization_id != organization_id or existing.service_order_id != order.id:
            raise ValueError("ledger idempotency key belongs to another tenant/order")
        return existing
    entry = ServiceOrderLedgerEntry(
        organization_id=organization_id, service_order_id=order.id, entry_type=entry_type,
        amount=_money(amount), currency=str(kwargs.pop("currency", "MXN")).upper(),
        idempotency_key=idempotency_key, **kwargs,
    )
    db.add(entry)
    db.flush()
    if db.query(ServiceOrderFinancial).filter_by(
        service_order_id=order.id, organization_id=organization_id,
    ).first():
        sync_financial_account_projection(db, order, organization_id=organization_id)
    return entry


def calculate_order_balance(db: Session, order, *, organization_id: int) -> dict:
    _require_order_scope(order, organization_id)
    entries = db.query(ServiceOrderLedgerEntry).filter_by(
        service_order_id=order.id, organization_id=organization_id, status="CONFIRMED",
    ).all()
    charges = sum((_money(item.amount) for item in entries if item.entry_type in CHARGE_TYPES), Decimal("0"))
    payments = sum((_money(item.amount) for item in entries if item.entry_type in PAYMENT_TYPES), Decimal("0"))
    fees = sum((_money(item.amount) for item in entries if item.entry_type in {"PROCESSING_FEE", "PLATFORM_FEE"}), Decimal("0"))
    refunds = sum((_money(item.amount) for item in entries if item.entry_type == "REFUND"), Decimal("0"))
    return {"charges": charges, "payments": payments, "fees": fees, "refunds": refunds,
            "outstanding_balance": max(Decimal("0"), charges - payments - refunds)}


def sync_financial_account_projection(db: Session, order, *, organization_id: int, financial_status: str | None = None):
    """Persist the ledger-derived amounts without creating a second balance source."""
    _require_order_scope(order, organization_id)
    account = db.query(ServiceOrderFinancial).filter_by(
        service_order_id=order.id, organization_id=organization_id,
    ).first()
    if not account:
        raise ValueError("financial account not found")
    balance = calculate_order_balance(db, order, organization_id=organization_id)
    account.amount_due = balance["outstanding_balance"]
    account.amount_paid = balance["payments"]
    account.outstanding_balance = balance["outstanding_balance"]
    if financial_status is not None:
        account.financial_status = financial_status
    db.flush()
    return balance


def get_financial_snapshot(db: Session, order, *, organization_id: int) -> dict:
    """Read the tenant-scoped financial account and ledger-derived balance."""
    account = db.query(ServiceOrderFinancial).filter_by(
        service_order_id=order.id, organization_id=organization_id,
    ).first()
    if not account:
        raise ValueError("financial account not found")
    balance = calculate_order_balance(db, order, organization_id=organization_id)
    return {"account": account, "balance": balance, "financial_status": account.financial_status}


def record_visit_payment(db: Session, payment, *, provider_payload: dict):
    """Apply a confirmed visit payment to the order ledger exactly once."""
    from app.models.service_order import ServiceOrder

    if payment.payment_type != "TECHNICAL_VISIT":
        raise ValueError("payment is not a technical visit")
    order = db.query(ServiceOrder).filter(ServiceOrder.id == payment.service_order_id).first()
    if not order or payment.organization_id != order.organization_id:
        raise ValueError("payment service order tenant mismatch")
    snapshot = db.query(VisitPricingSnapshot).filter_by(
        service_order_id=order.id, organization_id=payment.organization_id,
    ).first()
    account = db.query(ServiceOrderFinancial).filter_by(
        service_order_id=order.id, organization_id=payment.organization_id,
    ).first()
    if not snapshot or not account:
        raise ValueError("visit financial records are incomplete")

    provider_currency = str(provider_payload.get("currency") or "").upper()
    provider_amount_minor = provider_payload.get("amount_received")
    if provider_amount_minor is None:
        provider_amount_minor = provider_payload.get("amount_total")
    expected_currency = str(payment.currency or snapshot.currency or "").upper()
    expected_minor = int((_money(snapshot.total_amount) * 100).to_integral_value())
    try:
        provider_amount_minor = int(provider_amount_minor)
    except (TypeError, ValueError):
        raise ValueError("visit payment amount is invalid") from None
    if provider_currency != expected_currency or provider_amount_minor != expected_minor:
        raise ValueError("visit payment amount or currency mismatch")

    entry = append_ledger_entry(
        db, order, organization_id=payment.organization_id, entry_type="VISIT_PAYMENT",
        amount=snapshot.total_amount, currency=snapshot.currency, payment_method="STRIPE_CARD",
        payment_id=payment.id, external_reference=provider_payload.get("id"),
        idempotency_key=f"visit-payment:{payment.id}",
    )
    account.visit_fee = _money(snapshot.total_amount)
    balance = sync_financial_account_projection(
        db, order, organization_id=payment.organization_id,
        financial_status="VISIT_PAID" if calculate_order_balance(
            db, order, organization_id=payment.organization_id,
        )["outstanding_balance"] <= 0 else "PARTIALLY_PAID",
    )
    return entry


def calculate_marketplace_fee(amount, *, order_origin: str, policy: OrganizationPaymentPolicy) -> Decimal:
    origin = str(order_origin or "").upper()
    if origin == "PRIVATE":
        rate = Decimal("0")
    elif origin in {"MARKETPLACE", "MARKETPLACE_ESCALATED"}:
        rate = _money(policy.marketplace_fee_rate) / Decimal("100")
    else:
        raise ValueError("invalid order origin")
    return (_money(amount) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_provider_earning(amount, platform_fee, processing_fee=0) -> Decimal:
    return max(Decimal("0"), _money(amount) - _money(platform_fee) - _money(processing_fee))


def payment_schedule(amount, *, policy: OrganizationPaymentPolicy) -> list[dict]:
    """Return configured collection stages without creating a payment."""
    total = _money(amount)
    if total <= _money(policy.small_service_limit):
        percentages = [("FULL", Decimal("100"))]
    elif total <= _money(policy.medium_service_limit):
        percentages = [("DEPOSIT", _money(policy.medium_deposit_percentage)),
                       ("COMPLETION", Decimal("100") - _money(policy.medium_deposit_percentage))]
    else:
        percentages = [("STAGE_1", _money(policy.large_stage_1_percentage)),
                       ("STAGE_2", _money(policy.large_stage_2_percentage)),
                       ("STAGE_3", _money(policy.large_stage_3_percentage))]
    stages = []
    allocated = Decimal("0")
    for index, (name, percentage) in enumerate(percentages):
        stage_amount = total - allocated if index == len(percentages) - 1 else (total * percentage / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        allocated += stage_amount
        stages.append({"stage": name, "percentage": percentage, "amount": stage_amount})
    return stages


def can_dispatch_service_order(order, *, financial_account: ServiceOrderFinancial, policy: OrganizationPaymentPolicy, balance: dict) -> bool:
    if is_pricing_unavailable(order, financial_account):
        return False
    if getattr(financial_account, "pricing_status", "UNKNOWN") == "WAIVED":
        return True
    if financial_account.order_origin in {"MARKETPLACE", "MARKETPLACE_ESCALATED"} and policy.visit_payment_timing == "PREPAID":
        return any(value > 0 for key, value in balance.items() if key == "payments") and balance["outstanding_balance"] <= 0
    if policy.visit_payment_timing == "ON_ARRIVAL":
        return True
    if policy.visit_payment_timing == "POSTPAID":
        return True
    return financial_account.financial_status in {"VISIT_PAID", "PAID"} and balance["outstanding_balance"] <= 0
