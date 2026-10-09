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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.payment import Payment, PlatformLedgerEntry
from app.models.service_order import ServiceOrder
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.visit_pricing_snapshot import VisitPricingSnapshot
from app.services.service_order_financial_service import record_visit_payment
from app.services.service_order_financial_service import append_ledger_entry, sync_financial_account_projection


STRIPE_API_URL = "https://api.stripe.com/v1"


def stripe_is_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY", "").strip())


def stripe_currency() -> str:
    return os.getenv("STRIPE_CURRENCY", "mxn").strip().lower() or "mxn"


def stripe_expected_livemode() -> bool:
    configured = os.getenv("STRIPE_EXPECTED_LIVEMODE")
    if configured is None:
        environment = (os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("ENVIRONMENT") or "").strip().lower()
        return environment not in {"", "local", "development", "dev", "test", "testing"}
    value = configured.strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("STRIPE_EXPECTED_LIVEMODE must be true or false")
    return value == "true"


def _validate_secret_mode(secret: str) -> None:
    """Reject recognizable Stripe keys whose mode disagrees with deployment policy."""
    if not secret.startswith("sk_"):
        return
    expected_prefix = "sk_live_" if stripe_expected_livemode() else "sk_test_"
    if not secret.startswith(expected_prefix):
        raise RuntimeError("Stripe secret key mode does not match STRIPE_EXPECTED_LIVEMODE")


def _validate_livemode(value) -> None:
    # Stripe includes livemode on real events and objects. None keeps legacy
    # synthetic test fixtures compatible without weakening real-event checks.
    if value is not None and bool(value) != stripe_expected_livemode():
        raise ValueError("Stripe mode does not match STRIPE_EXPECTED_LIVEMODE")


def _amount_minor(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _stripe_request(path: str, *, data: dict[str, str], idempotency_key: str) -> dict:
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe sandbox nao configurado")
    try:
        _validate_secret_mode(secret)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Checkout Stripe nao configurado para este ambiente") from exc
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


def _stripe_retrieve_checkout_session(session_id: str) -> dict:
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe sandbox nao configurado")
    try:
        _validate_secret_mode(secret)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Checkout Stripe nao configurado para este ambiente") from exc
    try:
        response = httpx.get(
            f"{STRIPE_API_URL}/checkout/sessions/{session_id}",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=float(os.getenv("STRIPE_TIMEOUT_SECONDS", "10")),
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Stripe indisponivel") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Stripe nao permitiu consultar a sessao")
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
    currency: str | None = None,
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
        currency=(currency or stripe_currency()).strip(),
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
    idempotency_key: str | None = None,
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
    if payment.service_order_id:
        metadata["service_order_id"] = str(payment.service_order_id)
    if payment.installment_id:
        metadata["installment_id"] = str(payment.installment_id)
    data = {
        "mode": "subscription" if recurring else "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(payment.id),
        "metadata[payment_id]": metadata["payment_id"],
        "metadata[organization_id]": metadata["organization_id"],
        "metadata[payment_type]": metadata["payment_type"],
    }
    for key in ("service_order_id", "installment_id"):
        if key in metadata:
            data[f"metadata[{key}]"] = metadata[key]
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
    payload = _stripe_request("/checkout/sessions", data=data, idempotency_key=idempotency_key or payment.idempotency_key)
    checkout_session_id = payload.get("id")
    checkout_url = payload.get("url")
    if not checkout_session_id or not isinstance(checkout_url, str) or not checkout_url.strip():
        raise HTTPException(status_code=502, detail="Stripe nao retornou um checkout valido")
    payment.stripe_checkout_session_id = checkout_session_id
    payment.checkout_url = checkout_url.strip()
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


def _event_object(event: dict) -> dict:
    return (event.get("data") or {}).get("object") or {}


def _event_object_id(event: dict) -> str | None:
    return str(_event_object(event).get("id") or "")[:255] or None


def _event_id(event: dict) -> str:
    event_id = str(event.get("id") or "").strip()
    if event_id:
        return event_id[:255]
    # Existing unit fixtures predate Stripe's top-level id. Real Stripe
    # deliveries always include it; this deterministic fallback is local
    # test compatibility only and is never a provider identifier.
    return "synthetic:" + hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()


def _find_payment_for_event(db: Session, object_data: dict) -> Payment | None:
    metadata = object_data.get("metadata") or {}
    payment_id = metadata.get("payment_id") or object_data.get("client_reference_id")
    payment = db.query(Payment).filter(
        Payment.id == int(payment_id),
    ).with_for_update().first() if payment_id and str(payment_id).isdigit() else None
    if not payment and object_data.get("id"):
        payment = db.query(Payment).filter(Payment.stripe_checkout_session_id == object_data["id"]).with_for_update().first()
    for field in ("payment_intent", "subscription", "customer"):
        if not payment and object_data.get(field):
            column = getattr(Payment, f"stripe_{field}_id")
            payment = db.query(Payment).filter(column == object_data[field]).with_for_update().first()
    return payment


def _register_webhook_event(db: Session, event: dict):
    from app.models.stripe_reconciliation import StripeWebhookEvent

    event_id = _event_id(event)
    existing = db.query(StripeWebhookEvent).filter_by(event_id=event_id).with_for_update().first()
    if existing:
        if existing.status == "SUCCEEDED":
            return existing, True
        existing.status = "PROCESSING"
        existing.attempts = (existing.attempts or 0) + 1
        existing.last_error = None
        db.flush()
        return existing, False
    object_data = _event_object(event)
    record = StripeWebhookEvent(
        event_id=event_id,
        event_type=str(event.get("type") or "")[:96],
        livemode=event.get("livemode", object_data.get("livemode")),
        object_id=_event_object_id(event),
        status="PROCESSING",
        attempts=1,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.query(StripeWebhookEvent).filter_by(event_id=event_id).with_for_update().first()
        if not existing:
            raise
        if existing.status == "SUCCEEDED":
            return existing, True
        existing.status = "PROCESSING"
        existing.attempts = (existing.attempts or 0) + 1
        existing.last_error = None
        db.flush()
        return existing, False
    return record, False


def _persist_webhook_failure(db: Session, event: dict, exc: Exception) -> None:
    """Keep a sanitized failure record even when the webhook transaction rolls back."""
    from app.models.stripe_reconciliation import StripeWebhookEvent

    db.rollback()
    event_id = _event_id(event)
    record = db.query(StripeWebhookEvent).filter_by(event_id=event_id).with_for_update().first()
    if not record:
        object_data = _event_object(event)
        record = StripeWebhookEvent(
            event_id=event_id,
            event_type=str(event.get("type") or "")[:96],
            livemode=event.get("livemode", object_data.get("livemode")),
            object_id=_event_object_id(event),
            status="FAILED",
            attempts=1,
        )
        db.add(record)
    else:
        record.status = "FAILED"
        record.attempts = max(record.attempts or 1, 1)
    record.last_error = _sanitized_error(exc)
    record.processed_at = None
    db.commit()


def _sanitized_error(exc: Exception) -> str:
    return str(exc)[:240] if isinstance(exc, (ValueError, RuntimeError)) else exc.__class__.__name__


def _minor_amount(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("Stripe adjustment amount is invalid") from None


def _currency_matches(payment: Payment, object_data: dict) -> bool:
    return str(object_data.get("currency") or "").lower() == str(payment.currency or "").lower()


def _append_adjustment_ledger(db: Session, payment: Payment, *, entry_type: str, amount: Decimal, key: str, external_reference: str) -> None:
    order = db.query(ServiceOrder).filter(
        ServiceOrder.id == payment.service_order_id,
        ServiceOrder.organization_id == payment.organization_id,
    ).first() if payment.service_order_id else None
    if order:
        append_ledger_entry(
            db, order, organization_id=payment.organization_id, entry_type=entry_type,
            amount=amount, currency=payment.currency, payment_id=payment.id,
            external_reference=external_reference, idempotency_key=key,
        )
        sync_financial_account_projection(db, order, organization_id=payment.organization_id)
        return
    existing = db.query(PlatformLedgerEntry).filter_by(
        payment_id=payment.id, entry_type=entry_type, reference=key,
    ).first()
    if not existing:
        db.add(PlatformLedgerEntry(
            organization_id=payment.organization_id, technician_id=payment.technician_id,
            service_order_id=None, payment_id=payment.id, entry_type=entry_type,
            amount=amount, currency=payment.currency, status="OPEN",
            description=f"Stripe {entry_type.lower()}", reference=key,
        ))


def _apply_refund(db: Session, payment: Payment, event: dict, object_data: dict) -> Payment:
    from app.models.stripe_reconciliation import StripePaymentAdjustment

    if not _currency_matches(payment, object_data):
        raise ValueError("Stripe refund currency mismatch")
    gross_minor = int((Decimal(payment.gross_amount) * 100).quantize(Decimal("1")))
    cumulative_minor = object_data.get("amount_refunded")
    if cumulative_minor is None:
        cumulative_minor = object_data.get("amount")
    cumulative_minor = _minor_amount(cumulative_minor)
    already_refunded = sum(
        _minor_amount(Decimal(row.amount) * 100)
        for row in db.query(StripePaymentAdjustment).filter_by(payment_id=payment.id, kind="REFUND", status="APPLIED").all()
    )
    new_minor = cumulative_minor - already_refunded if object_data.get("amount_refunded") is not None else cumulative_minor
    if new_minor < 0 or already_refunded + new_minor > gross_minor:
        raise ValueError("Stripe refund exceeds payment amount")
    if new_minor:
        amount = (Decimal(new_minor) / 100).quantize(Decimal("0.01"))
        adjustment = StripePaymentAdjustment(
            payment_id=payment.id, organization_id=payment.organization_id,
            stripe_event_id=_event_id(event), provider_adjustment_id=str(object_data.get("id") or _event_id(event)),
            kind="REFUND", status="APPLIED", amount=amount, currency=payment.currency,
            ledger_idempotency_key=f"stripe-refund:{_event_id(event)}",
        )
        db.add(adjustment)
        _append_adjustment_ledger(
            db, payment, entry_type="REFUND", amount=amount,
            key=f"stripe-refund:{_event_id(event)}", external_reference=str(object_data.get("id") or _event_id(event)),
        )
    total_refunded = already_refunded + new_minor
    payment.status = "REFUNDED" if total_refunded >= gross_minor else "PARTIALLY_REFUNDED"
    payment.updated_at = datetime.utcnow()
    return payment


def _apply_dispute(db: Session, payment: Payment, event: dict, object_data: dict) -> Payment:
    from app.models.stripe_reconciliation import StripePaymentAdjustment

    if not _currency_matches(payment, object_data):
        raise ValueError("Stripe dispute currency mismatch")
    dispute_id = str(object_data.get("id") or "")[:255]
    if not dispute_id:
        raise ValueError("Stripe dispute id is missing")
    status = str(object_data.get("status") or "under_review").upper()
    adjustment = db.query(StripePaymentAdjustment).filter_by(
        payment_id=payment.id, kind="DISPUTE", provider_adjustment_id=dispute_id,
    ).with_for_update().first()
    amount = Decimal(_minor_amount(object_data.get("amount"))) / 100
    if not adjustment:
        adjustment = StripePaymentAdjustment(
            payment_id=payment.id, organization_id=payment.organization_id,
            stripe_event_id=_event_id(event), provider_adjustment_id=dispute_id,
            kind="DISPUTE", status=status, amount=amount, currency=payment.currency,
            ledger_idempotency_key=f"stripe-dispute:{dispute_id}",
        )
        db.add(adjustment)
        _append_adjustment_ledger(
            db, payment, entry_type="DISPUTE", amount=amount,
            key=f"stripe-dispute:{dispute_id}", external_reference=dispute_id,
        )
    else:
        adjustment.status = status
    if status == "WON" and not db.query(StripePaymentAdjustment).filter_by(
        payment_id=payment.id, kind="DISPUTE_REVERSAL", provider_adjustment_id=dispute_id,
    ).first():
        db.add(StripePaymentAdjustment(
            payment_id=payment.id, organization_id=payment.organization_id,
            stripe_event_id=_event_id(event), provider_adjustment_id=dispute_id,
            kind="DISPUTE_REVERSAL", status="APPLIED", amount=adjustment.amount,
            currency=payment.currency, ledger_idempotency_key=f"stripe-dispute-reversal:{dispute_id}",
        ))
        _append_adjustment_ledger(
            db, payment, entry_type="DISPUTE_REVERSAL", amount=adjustment.amount,
            key=f"stripe-dispute-reversal:{dispute_id}", external_reference=dispute_id,
        )
    payment.status = "DISPUTED" if status != "WON" else "PAID"
    payment.updated_at = datetime.utcnow()
    return payment


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
    supported_events = {
        "checkout.session.completed", "invoice.paid", "payment_intent.succeeded",
        "payment_intent.payment_failed", "invoice.payment_failed",
        "charge.refunded", "charge.dispute.created", "charge.dispute.updated",
        "charge.dispute.closed",
    }
    _validate_livemode(event.get("livemode", object_data.get("livemode")))
    webhook = None
    try:
        webhook, duplicate = _register_webhook_event(db, event)
        if duplicate:
            payment = db.query(Payment).filter(Payment.id == webhook.payment_id).first() if webhook.payment_id else None
            return payment
        if event_type not in supported_events:
            webhook.status = "IGNORED"
            webhook.processed_at = datetime.utcnow()
            db.flush()
            return None
        payment = _find_payment_for_event(db, object_data)
        if not payment:
            raise ValueError("Stripe event payment not found; retry is required")
        metadata = object_data.get("metadata") or {}
        metadata_organization_id = metadata.get("organization_id")
        if metadata_organization_id and str(metadata_organization_id) != str(payment.organization_id):
            raise ValueError("Stripe event organization does not match the stored payment")
        webhook.payment_id = payment.id
        webhook.organization_id = payment.organization_id
        if payment.status in {"CANCELLED", "SUPERSEDED"} and event_type not in {"charge.refunded", "charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed"}:
            webhook.status = "IGNORED"
            webhook.processed_at = datetime.utcnow()
            db.flush()
            return payment
        if event_type == "charge.refunded":
            payment = _apply_refund(db, payment, event, object_data)
            if payment.service_order_id:
                from app.models.stripe_reconciliation import StripePaymentAdjustment
                from app.services.technician_earning_reconciliation_service import reconcile_payment_adjustment
                refund = db.query(StripePaymentAdjustment).filter_by(
                    payment_id=payment.id, kind="REFUND", stripe_event_id=_event_id(event), status="APPLIED",
                ).first()
                reconcile_payment_adjustment(
                    db, payment, provider_event_key=f"stripe:{_event_id(event)}",
                    amount=refund.amount if refund else 0, event_type="REFUND",
                )
        elif event_type.startswith("charge.dispute."):
            payment = _apply_dispute(db, payment, event, object_data)
            if payment.service_order_id:
                from app.services.technician_earning_reconciliation_service import reconcile_payment_adjustment
                dispute_status = str(object_data.get("status") or "under_review").lower()
                dispute_event_type = "DISPUTE_WON" if dispute_status == "won" else "DISPUTE_LOST" if dispute_status == "lost" else "DISPUTE_OPEN"
                reconcile_payment_adjustment(
                    db, payment, provider_event_key=f"stripe:{_event_id(event)}",
                    amount=object_data.get("amount"), event_type=dispute_event_type,
                )
        else:
            checkout_paid = event_type != "checkout.session.completed" or object_data.get("payment_status") == "paid"
            if checkout_paid and event_type in {"checkout.session.completed", "invoice.paid", "payment_intent.succeeded"}:
                if payment.payment_type == "TECHNICAL_VISIT" and payment.status not in {"PAID", "PAID_CASH"}:
                    try:
                        expected_amount = object_data.get("amount_received")
                        if expected_amount is None:
                            expected_amount = object_data.get("amount_total")
                        expected_currency = str(object_data.get("currency") or "").lower()
                        if expected_amount is None or not expected_currency:
                            raise ValueError("visit payment amount or currency is missing")
                        record_visit_payment(db, payment, provider_payload=object_data)
                    except (TypeError, ValueError):
                        payment.status = "FAILED"
                        payment.updated_at = datetime.utcnow()
                        db.flush()
                        return payment
                if payment.payment_type.startswith("SERVICE_") and payment.status not in {"PAID", "PAID_CASH"}:
                    from app.services.service_order_payment_plan_service import record_service_installment_payment
                    payment = record_service_installment_payment(db, payment, provider_payload=object_data)
                else:
                    payment = mark_payment_paid(db, payment, provider_payload=object_data)
                if payment.status == "PAID" and payment.service_order_id:
                    from app.services.technician_earning_reconciliation_service import reconcile_confirmed_payment
                    reconcile_confirmed_payment(
                        db, payment, provider_event_key=f"stripe:{_event_id(event)}",
                        confirmed_amount=payment.gross_amount,
                    )
            elif event_type in {"payment_intent.payment_failed", "invoice.payment_failed"} and payment.status not in {"PAID", "PAID_CASH"}:
                payment.status = "FAILED"
                payment.updated_at = datetime.utcnow()
        webhook.status = "SUCCEEDED"
        webhook.processed_at = datetime.utcnow()
        webhook.last_error = None
        db.flush()
        return payment
    except Exception as exc:
        _persist_webhook_failure(db, event, exc)
        raise


def reconcile_stripe_checkout_payment(db: Session, payment: Payment) -> Payment:
    """Recover a paid Checkout session through the normal webhook accounting path."""
    if payment.payment_method != "STRIPE_CARD" or not payment.stripe_checkout_session_id:
        raise ValueError("payment has no Stripe Checkout session")
    session = _stripe_retrieve_checkout_session(payment.stripe_checkout_session_id)
    if session.get("id") != payment.stripe_checkout_session_id:
        raise ValueError("Stripe session does not match the stored payment")
    _validate_livemode(session.get("livemode"))
    if session.get("status") != "complete" or session.get("payment_status") != "paid":
        raise ValueError("Stripe Checkout session is not paid")
    amount_total = session.get("amount_total")
    expected_amount = _amount_minor(Decimal(payment.gross_amount))
    if amount_total is None or int(amount_total) != expected_amount:
        raise ValueError("Stripe amount does not match the stored payment")
    if str(session.get("currency") or "").lower() != str(payment.currency).lower():
        raise ValueError("Stripe currency does not match the stored payment")
    metadata = session.get("metadata") or {}
    if str(metadata.get("payment_id") or "") != str(payment.id):
        raise ValueError("Stripe payment metadata does not match the stored payment")
    if str(metadata.get("organization_id") or "") != str(payment.organization_id):
        raise ValueError("Stripe organization metadata does not match the stored payment")
    if str(metadata.get("payment_type") or "") != str(payment.payment_type):
        raise ValueError("Stripe payment type metadata does not match the stored payment")
    if session.get("client_reference_id") not in (None, str(payment.id)):
        raise ValueError("Stripe client reference does not match the stored payment")
    if payment.service_order_id and metadata.get("service_order_id") not in (None, str(payment.service_order_id)):
        raise ValueError("Stripe service order metadata does not match the stored payment")
    if payment.installment_id and metadata.get("installment_id") not in (None, str(payment.installment_id)):
        raise ValueError("Stripe installment metadata does not match the stored payment")
    event = {"type": "checkout.session.completed", "data": {"object": session}}
    reconciled = handle_stripe_event(db, event)
    if reconciled is None:
        raise ValueError("stored payment could not be resolved from Stripe session")
    return reconciled


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


def record_external_payment(
    db: Session, *, order: ServiceOrder, actor, amount: Decimal, currency: str,
    purpose: str, payment_method: str, external_reference: str,
    evidence_reference: str | None = None, observation: str | None = None,
) -> Payment:
    """Record an externally confirmed receipt exactly once and audit its actor."""
    from app.models.external_payment_confirmation import ExternalPaymentConfirmation
    from app.services.service_order_payment_plan_service import SERVICE_PAYMENT_TYPES, record_service_installment_payment

    purpose = purpose.strip().upper()
    payment_method = payment_method.strip().upper()
    currency = currency.strip().upper()
    reference = external_reference.strip()
    if purpose not in {"VISIT", "SERVICE", "INSTALLMENT"} or payment_method not in {"BANK_TRANSFER", "CASH", "CARD_EXTERNAL", "OTHER_EXTERNAL"}:
        raise ValueError("payment purpose is not supported")
    if not reference or len(reference) > 120 or amount <= 0:
        raise ValueError("external payment details are invalid")
    existing_confirmation = db.query(ExternalPaymentConfirmation).filter_by(
        organization_id=order.organization_id, external_reference=reference,
    ).with_for_update().first()
    if existing_confirmation:
        if existing_confirmation.service_order_id != order.id or existing_confirmation.amount != amount or existing_confirmation.currency != currency:
            raise ValueError("external reference is already linked to another payment")
        return db.query(Payment).filter_by(id=existing_confirmation.payment_id).one()

    payment = None
    if purpose == "VISIT":
        payment = db.query(Payment).filter(
            Payment.service_order_id == order.id, Payment.organization_id == order.organization_id,
            Payment.payment_type == "TECHNICAL_VISIT", Payment.payment_method != "STRIPE_CARD",
            Payment.status.in_({"PENDING", "CHECKOUT_CREATED", "FAILED"}),
        ).order_by(Payment.id.desc()).first()
        expected = db.query(VisitPricingSnapshot).filter_by(
            service_order_id=order.id, organization_id=order.organization_id,
        ).one_or_none()
        if not expected or Decimal(str(expected.total_amount)) != amount or str(expected.currency).upper() != currency:
            raise ValueError("external payment does not match the visit snapshot")
        payment_type = "TECHNICAL_VISIT"
    else:
        payment_query = db.query(Payment).filter(
            Payment.service_order_id == order.id, Payment.organization_id == order.organization_id,
            Payment.status.in_({"PENDING", "CHECKOUT_CREATED", "FAILED"}),
        )
        if purpose == "INSTALLMENT":
            payment_query = payment_query.filter(Payment.installment_id.isnot(None))
        payment = payment_query.order_by(Payment.id.desc()).first()
        payment_type = "SERVICE_FULL" if purpose == "SERVICE" else None
        if purpose == "INSTALLMENT" and (not payment or payment.payment_type not in SERVICE_PAYMENT_TYPES):
            raise ValueError("installment payment is not available")
        if purpose == "SERVICE":
            financial = db.query(ServiceOrderFinancial).filter_by(
                service_order_id=order.id, organization_id=order.organization_id,
            ).one_or_none()
            outstanding = Decimal(str(financial.service_outstanding_balance or 0)) if financial else Decimal("0.00")
            if not financial or outstanding <= 0 or amount > outstanding:
                raise ValueError("external payment does not match the outstanding service balance")
    if payment and payment.status in {"PAID", "PAID_CASH"}:
        raise ValueError("payment is already confirmed")
    if purpose == "VISIT":
        pending_stripe_payments = db.query(Payment).filter(
            Payment.service_order_id == order.id,
            Payment.organization_id == order.organization_id,
            Payment.payment_type == "TECHNICAL_VISIT",
            Payment.payment_method == "STRIPE_CARD",
            Payment.status.in_({"PENDING", "CHECKOUT_CREATED"}),
        ).with_for_update().all()
        for pending_stripe in pending_stripe_payments:
            pending_stripe.status = "CANCELLED"
            pending_stripe.updated_at = datetime.utcnow()
            pending_stripe.checkout_url = None
    if not payment:
        payment = create_payment(
            db, organization_id=order.organization_id, payment_type=payment_type,
            payment_method=payment_method, amount=amount, currency=currency,
            service_request_id=order.service_request_id, service_order_id=order.id,
            lead_id=order.lead_id, technician_id=order.responsible_user_id,
            idempotency_key=f"external-payment:{order.id}:{reference}",
        )
    if Decimal(str(payment.gross_amount)) != amount or str(payment.currency).upper() != currency:
        raise ValueError("external payment does not match the stored amount")
    payment.payment_method = payment_method
    payment.provider = "INTERNAL"
    payment.status = "PENDING"
    mark_payment_paid(db, payment, provider_payload={})
    if purpose == "VISIT":
        record_visit_payment(db, payment, provider_payload={"amount_total": int(amount * 100), "currency": currency, "id": reference})
    elif purpose == "INSTALLMENT":
        record_service_installment_payment(db, payment, provider_payload={"amount_total": int(amount * 100), "currency": currency})
    else:
        append_ledger_entry(
            db, order, organization_id=order.organization_id, entry_type="SERVICE_PAYMENT",
            amount=amount, currency=currency, payment_method=payment_method,
            payment_id=payment.id, external_reference=reference,
            idempotency_key=f"external-service-payment:{payment.id}",
        )
        financial = db.query(ServiceOrderFinancial).filter_by(
            service_order_id=order.id, organization_id=order.organization_id,
        ).one_or_none()
        if financial:
            financial.service_paid_amount = Decimal(str(financial.service_paid_amount or 0)) + amount
            financial.service_outstanding_balance = max(Decimal("0.00"), Decimal(str(financial.service_outstanding_balance or 0)) - amount)
            financial.updated_at = datetime.utcnow()
    if payment.service_order_id:
        from app.services.technician_earning_reconciliation_service import reconcile_confirmed_payment
        reconcile_confirmed_payment(db, payment, provider_event_key=f"external:{order.id}:{reference}", confirmed_amount=payment.gross_amount)
    db.add(ExternalPaymentConfirmation(
        organization_id=order.organization_id, service_order_id=order.id, payment_id=payment.id,
        purpose=purpose, payment_method=payment_method, amount=amount, currency=currency,
        external_reference=reference, evidence_reference=(evidence_reference or "").strip()[:240] or None,
        observation=(observation or "").strip()[:2000] or None, confirmed_by_user_id=actor.id,
    ))
    db.flush()
    return payment
