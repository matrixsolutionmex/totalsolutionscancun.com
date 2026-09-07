from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import os

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.service_order import ServiceOrder
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.service_order_payment_plan import ServiceOrderPaymentInstallment, ServiceOrderPaymentPlan
from app.models.service_order_quote import ServiceOrderQuote
from app.services.service_order_financial_service import append_ledger_entry, payment_schedule, resolve_payment_policy

MONEY = Decimal("0.01")
SERVICE_PAYMENT_TYPES = {"SERVICE_FULL", "SERVICE_DEPOSIT", "SERVICE_COMPLETION", "SERVICE_STAGE_1", "SERVICE_STAGE_2", "SERVICE_STAGE_3"}
PUBLIC_INSTALLMENT_TYPES = {
    "SERVICE_FULL": "FULL",
    "SERVICE_DEPOSIT": "DEPOSIT",
    "SERVICE_COMPLETION": "FINAL",
    "SERVICE_STAGE_1": "DEPOSIT",
    "SERVICE_STAGE_2": "PROGRESS",
    "SERVICE_STAGE_3": "FINAL",
}


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _policy_snapshot(policy) -> dict:
    return {name: str(getattr(policy, name)) for name in (
        "small_service_limit", "medium_service_limit", "medium_deposit_percentage",
        "large_stage_1_percentage", "large_stage_2_percentage", "large_stage_3_percentage",
    )} | {"organization_id": policy.organization_id, "currency": policy.currency}


def create_payment_plan_for_approved_quote(db: Session, order, quote) -> ServiceOrderPaymentPlan:
    if quote.status != "APPROVED" or quote.service_order_id != order.id or quote.organization_id != order.organization_id:
        raise HTTPException(status_code=409, detail="Orçamento aprovado inválido")
    existing = db.query(ServiceOrderPaymentPlan).filter_by(service_order_id=order.id, quote_id=quote.id, quote_version=quote.version).first()
    if existing:
        return existing
    policy = resolve_payment_policy(db, organization_id=order.organization_id)
    total = _money(quote.approved_total or quote.total)
    if total <= 0:
        raise HTTPException(status_code=409, detail="Orçamento aprovado sem valor")
    plan = ServiceOrderPaymentPlan(
        service_order_id=order.id, organization_id=order.organization_id, quote_id=quote.id,
        quote_version=quote.version, currency=str(quote.currency).upper(), approved_total=total,
        policy_snapshot=_policy_snapshot(policy), status="ACTIVE",
    )
    db.add(plan)
    db.flush()
    for index, stage in enumerate(payment_schedule(total, policy=policy), start=1):
        type_name = {"FULL": "SERVICE_FULL", "DEPOSIT": "SERVICE_DEPOSIT", "COMPLETION": "SERVICE_COMPLETION"}.get(stage["stage"], f"SERVICE_{stage['stage']}")
        db.add(ServiceOrderPaymentInstallment(
            payment_plan_id=plan.id, service_order_id=order.id, organization_id=order.organization_id,
            sequence=index, installment_type=type_name, percentage=stage["percentage"], amount=stage["amount"],
            currency=str(quote.currency).upper(), status="AVAILABLE" if index == 1 else "PENDING",
            due_trigger="QUOTE_APPROVED" if index == 1 else "EXPLICIT_RELEASE",
        ))
    financial = db.query(ServiceOrderFinancial).filter_by(service_order_id=order.id, organization_id=order.organization_id).first()
    if financial:
        financial.approved_quote_amount = total
        financial.service_paid_amount = Decimal("0.00")
        financial.service_outstanding_balance = total
        financial.updated_at = datetime.utcnow()
    db.flush()
    return plan


def payment_plan_projection(db: Session, order) -> dict | None:
    plan = db.query(ServiceOrderPaymentPlan).filter(
        ServiceOrderPaymentPlan.service_order_id == order.id,
        ServiceOrderPaymentPlan.organization_id == order.organization_id,
        ServiceOrderPaymentPlan.status.in_({"ACTIVE", "COMPLETED"}),
    ).order_by(ServiceOrderPaymentPlan.id.desc()).first()
    if not plan:
        return None
    latest_quote = db.query(ServiceOrderQuote).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).order_by(ServiceOrderQuote.version.desc()).first()
    if not latest_quote or latest_quote.id != plan.quote_id or latest_quote.version != plan.quote_version or latest_quote.status != "APPROVED":
        return None
    installments = sorted(plan.installments, key=lambda item: item.sequence)
    paid = sum((_money(item.amount) for item in installments if item.status == "PAID"), Decimal("0.00"))
    return {
        "status": plan.status, "currency": plan.currency, "approved_total": plan.approved_total,
        "service_paid_total": paid, "service_outstanding_balance": max(Decimal("0.00"), _money(plan.approved_total) - paid),
        "installments": [{"sequence": item.sequence, "type": PUBLIC_INSTALLMENT_TYPES.get(item.installment_type, "OTHER"), "percentage": item.percentage, "amount": item.amount, "currency": item.currency, "status": item.status, "due_trigger": item.due_trigger, "paid_at": item.paid_at, "checkout_available": item.status in {"AVAILABLE", "PAYMENT_PENDING"}} for item in installments],
    }


def create_installment_checkout(db: Session, order, installment_sequence: int, *, tracking_token: str) -> Payment:
    plan = db.query(ServiceOrderPaymentPlan).filter_by(
        service_order_id=order.id, organization_id=order.organization_id, status="ACTIVE",
    ).order_by(ServiceOrderPaymentPlan.id.desc()).first()
    latest_quote = db.query(ServiceOrderQuote).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).order_by(ServiceOrderQuote.version.desc()).first()
    if not plan or not latest_quote or latest_quote.id != plan.quote_id or latest_quote.version != plan.quote_version or latest_quote.status != "APPROVED":
        raise HTTPException(status_code=409, detail="Orçamento aprovado não está vigente")
    installment = db.query(ServiceOrderPaymentInstallment).filter_by(sequence=installment_sequence, payment_plan_id=plan.id, service_order_id=order.id, organization_id=order.organization_id).first()
    if not installment or installment.payment_plan.status != "ACTIVE":
        raise HTTPException(status_code=404, detail="Parcela não encontrada")
    if installment.status == "PAID":
        raise HTTPException(status_code=409, detail="Parcela já paga")
    if installment.status not in {"AVAILABLE", "PAYMENT_PENDING"}:
        raise HTTPException(status_code=409, detail="Parcela ainda não está disponível")
    payment = db.query(Payment).filter_by(installment_id=installment.id).order_by(Payment.id.desc()).first()
    if not payment:
        payment = Payment(organization_id=order.organization_id, service_request_id=order.service_request_id, service_order_id=order.id, lead_id=order.lead_id, technician_id=order.responsible_user_id, installment_id=installment.id, payment_type=installment.installment_type, payment_method="STRIPE_CARD", currency=installment.currency, gross_amount=installment.amount, provider="STRIPE", idempotency_key=f"service-installment:{installment.id}", status="PENDING")
        db.add(payment)
        db.flush()
    if payment.stripe_checkout_session_id:
        return payment
    from app.services.payment_service import create_stripe_checkout
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=503, detail="Checkout publico não configurado")
    create_stripe_checkout(db, payment, success_url=f"{base_url}/seguimiento/{tracking_token}?payment=success", cancel_url=f"{base_url}/seguimiento/{tracking_token}?payment=cancelled", description=f"Total Solutions - parcela {installment.sequence}")
    installment.status = "PAYMENT_PENDING"
    db.flush()
    return payment


def record_service_installment_payment(db: Session, payment: Payment, *, provider_payload: dict) -> Payment:
    if payment.payment_type not in SERVICE_PAYMENT_TYPES or not payment.installment_id:
        raise ValueError("service installment payment expected")
    installment = db.query(ServiceOrderPaymentInstallment).filter_by(id=payment.installment_id, organization_id=payment.organization_id, service_order_id=payment.service_order_id).with_for_update().first()
    if not installment:
        raise ValueError("installment not found")
    if installment.status == "PAID" and payment.status == "PAID":
        return payment
    amount = provider_payload.get("amount_received") or provider_payload.get("amount_total")
    currency = str(provider_payload.get("currency") or "").upper()
    expected_minor = int(_money(installment.amount) * 100)
    if amount is None or int(amount) != expected_minor or currency != str(installment.currency).upper():
        payment.status = "FAILED"
        db.flush()
        return payment
    payment.status = "PAID"
    payment.paid_at = payment.paid_at or datetime.utcnow()
    payment.stripe_payment_intent_id = provider_payload.get("payment_intent") or payment.stripe_payment_intent_id
    installment.status = "PAID"
    installment.paid_at = installment.paid_at or payment.paid_at
    plan = installment.payment_plan
    paid_total = sum((_money(item.amount) for item in plan.installments if item.status == "PAID"), Decimal("0.00"))
    financial = db.query(ServiceOrderFinancial).filter_by(
        service_order_id=payment.service_order_id, organization_id=payment.organization_id,
    ).first()
    if financial:
        financial.service_paid_amount = paid_total
        financial.service_outstanding_balance = max(Decimal("0.00"), _money(plan.approved_total) - paid_total)
        financial.updated_at = datetime.utcnow()
    if all(item.status == "PAID" for item in plan.installments):
        plan.status = "COMPLETED"
    order = db.query(ServiceOrder).filter_by(id=payment.service_order_id, organization_id=payment.organization_id).first()
    append_ledger_entry(db, order, organization_id=payment.organization_id, entry_type="SERVICE_PAYMENT", amount=_money(payment.gross_amount), currency=payment.currency, payment_id=payment.id, payment_method=payment.payment_method, idempotency_key=f"service-payment:{payment.id}")
    db.flush()
    return payment
