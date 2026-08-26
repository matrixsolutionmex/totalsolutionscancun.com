"""Payment orchestration shared by service charges and plan upgrades."""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.payment import Payment, PlatformLedgerEntry


STRIPE_API_URL = "https://api.stripe.com/v1"


def stripe_is_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY", "").strip())


def stripe_currency() -> str:
    return os.getenv("STRIPE_CURRENCY", "mxn").strip().lower() or "mxn"


def _amount_minor(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _stripe_request(path: str, *, data: dict[str, str], idempotency_key: str) -> dict:
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe sandbox nao configurado")
    try:
        response = httpx.post(
            f"{STRIPE_API_URL}{path}",
            data=data,
            headers={"Authorization": f"Bearer {secret}", "Idempotency-Key": idempotency_key},
            timeout=float(os.getenv("STRIPE_TIMEOUT_SECONDS", "10")),
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Stripe indisponivel") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Stripe rejeitou a sessao de checkout")
    return payload


def create_payment(
    db: Session,
    *,
    organization_id: int,
    payment_type: str,
    payment_method: str,
    amount: Decimal,
    service_request_id: int | None = None,
    service_order_id: int | None = None,
    lead_id: int | None = None,
    technician_id: int | None = None,
    upgrade_intent_id: int | None = None,
    idempotency_key: str | None = None,
) -> Payment:
    key = idempotency_key or str(uuid4())
    existing = db.query(Payment).filter(Payment.idempotency_key == key).first()
    if existing:
        return existing
    payment = Payment(
        organization_id=organization_id,
        service_request_id=service_request_id,
        service_order_id=service_order_id,
        lead_id=lead_id,
        technician_id=technician_id,
        upgrade_intent_id=upgrade_intent_id,
        payment_type=payment_type,
        payment_method=payment_method,
        currency=stripe_currency(),
        gross_amount=Decimal(amount),
        provider="STRIPE" if payment_method == "STRIPE_CARD" else "INTERNAL",
        idempotency_key=key,
        status="PENDING",
    )
    db.add(payment)
    db.flush()
    return payment


def create_stripe_checkout(
    db: Session,
    payment: Payment,
    *,
    success_url: str,
    cancel_url: str,
    description: str,
    stripe_price_id: str | None = None,
    recurring: bool = False,
) -> Payment:
    if payment.payment_method != "STRIPE_CARD":
        raise HTTPException(status_code=400, detail="Pagamento nao usa checkout Stripe")
    if payment.stripe_checkout_session_id:
        return payment
    metadata = {
        "payment_id": str(payment.id),
        "organization_id": str(payment.organization_id),
        "payment_type": payment.payment_type,
    }
    data = {
        "mode": "subscription" if recurring else "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(payment.id),
        "metadata[payment_id]": metadata["payment_id"],
        "metadata[organization_id]": metadata["organization_id"],
        "metadata[payment_type]": metadata["payment_type"],
    }
    if stripe_price_id:
        data["line_items[0][price]"] = stripe_price_id
        data["line_items[0][quantity]"] = "1"
        payment.stripe_price_id = stripe_price_id
    else:
        data.update({
            "line_items[0][price_data][currency]": payment.currency,
            "line_items[0][price_data][unit_amount]": str(_amount_minor(Decimal(payment.gross_amount))),
            "line_items[0][price_data][product_data][name]": description,
            "line_items[0][quantity]": "1",
        })
        if recurring:
            data["line_items[0][price_data][recurring][interval]"] = "month"
    payload = _stripe_request("/checkout/sessions", data=data, idempotency_key=payment.idempotency_key)
    payment.stripe_checkout_session_id = payload.get("id")
    payment.checkout_url = payload.get("url")
    payment.status = "CHECKOUT_CREATED"
    payment.updated_at = datetime.utcnow()
    db.flush()
    return payment


def verify_stripe_signature(payload: bytes, signature: str | None) -> bool:
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret or not signature:
        return False
    values = {}
    for item in signature.split(","):
        key, _, value = item.partition("=")
        values.setdefault(key, []).append(value)
    timestamp = values.get("t", [""])[0]
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - timestamp_value) > 300:
        return False
    signed = f"{timestamp}.{payload.decode('utf-8')}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in values.get("v1", []))


def mark_payment_paid(db: Session, payment: Payment, *, provider_payload: dict) -> Payment:
    if payment.status in {"PAID", "PAID_CASH"}:
        return payment
    payment.status = "PAID"
    payment.paid_at = payment.paid_at or datetime.utcnow()
    payment.stripe_payment_intent_id = provider_payload.get("payment_intent") or payment.stripe_payment_intent_id
    payment.stripe_customer_id = provider_payload.get("customer") or payment.stripe_customer_id
    payment.stripe_subscription_id = provider_payload.get("subscription") or payment.stripe_subscription_id
    payment.platform_fee_amount = platform_fee_amount(Decimal(payment.gross_amount))
    if not db.query(PlatformLedgerEntry).filter(PlatformLedgerEntry.payment_id == payment.id).first():
        db.add_all([
            PlatformLedgerEntry(
                organization_id=payment.organization_id, technician_id=payment.technician_id,
                service_order_id=payment.service_order_id, payment_id=payment.id,
                entry_type="PAYMENT", amount=payment.gross_amount, currency=payment.currency,
                description="Pagamento confirmado pelo provider", reference=payment.idempotency_key,
            ),
            PlatformLedgerEntry(
                organization_id=payment.organization_id, technician_id=payment.technician_id,
                service_order_id=payment.service_order_id, payment_id=payment.id,
                entry_type="PLATFORM_FEE", amount=payment.platform_fee_amount, currency=payment.currency,
                description="Taxa Total Solutions devida", reference=payment.idempotency_key,
            ),
        ])
    payment.updated_at = datetime.utcnow()
    db.flush()
    return payment


def handle_stripe_event(db: Session, event: dict) -> Payment | None:
    event_type = event.get("type", "")
    object_data = (event.get("data") or {}).get("object") or {}
    metadata = object_data.get("metadata") or {}
    payment_id = metadata.get("payment_id") or object_data.get("client_reference_id")
    payment = db.query(Payment).filter(Payment.id == int(payment_id)).with_for_update().first() if payment_id and str(payment_id).isdigit() else None
    if not payment and object_data.get("id"):
        payment = db.query(Payment).filter(Payment.stripe_checkout_session_id == object_data["id"]).with_for_update().first()
    if not payment and object_data.get("payment_intent"):
        payment = db.query(Payment).filter(Payment.stripe_payment_intent_id == object_data["payment_intent"]).with_for_update().first()
    if not payment and object_data.get("subscription"):
        payment = db.query(Payment).filter(Payment.stripe_subscription_id == object_data["subscription"]).with_for_update().first()
    if not payment and object_data.get("customer"):
        payment = db.query(Payment).filter(Payment.stripe_customer_id == object_data["customer"]).with_for_update().first()
    if not payment:
        return None
    checkout_paid = event_type != "checkout.session.completed" or object_data.get("payment_status") == "paid"
    if checkout_paid and event_type in {"checkout.session.completed", "invoice.paid", "payment_intent.succeeded"}:
        return mark_payment_paid(db, payment, provider_payload=object_data)
    if event_type in {"payment_intent.payment_failed", "invoice.payment_failed"} and payment.status not in {"PAID", "PAID_CASH"}:
        payment.status = "FAILED"
        payment.updated_at = datetime.utcnow()
        db.flush()
    return payment


def platform_fee_amount(amount: Decimal) -> Decimal:
    fee_type = os.getenv("PLATFORM_FEE_TYPE", "PERCENTAGE").strip().upper()
    fee_value = Decimal(os.getenv("PLATFORM_FEE_VALUE", "0"))
    result = fee_value if fee_type == "FIXED" else amount * fee_value / Decimal("100")
    minimum = Decimal(os.getenv("PLATFORM_FEE_MINIMUM", "0"))
    maximum = os.getenv("PLATFORM_FEE_MAXIMUM", "").strip()
    result = max(result, minimum)
    if maximum:
        result = min(result, Decimal(maximum))
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def record_cash_payment(db: Session, payment: Payment) -> Payment:
    mark_payment_paid(db, payment, provider_payload={})
    payment.status = "PAID_CASH"
    fee = platform_fee_amount(Decimal(payment.gross_amount))
    payment.platform_fee_amount = fee
    for entry in db.query(PlatformLedgerEntry).filter(PlatformLedgerEntry.payment_id == payment.id).all():
        if entry.entry_type == "PLATFORM_FEE":
            entry.amount = fee
    db.flush()
    return payment
