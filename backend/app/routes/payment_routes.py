import json
from decimal import Decimal

import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db, require_admin_user
from app.models.payment import Payment
from app.models.service_order_payment_plan import ServiceOrderPaymentInstallment
from app.models.service_order import ServiceOrder
from app.models.service_request import ServiceRequest
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.visit_pricing_snapshot import VisitPricingSnapshot
from app.services.commercial_upgrade_service import activate_upgrade_from_paid_payment
from app.services.service_order_financial_service import resolve_payment_policy
from app.services.payment_service import (
    create_payment,
    handle_stripe_event,
    record_cash_payment,
    verify_stripe_signature,
    create_stripe_checkout,
    reconcile_stripe_checkout_payment,
)
from app.services.service_order_payment_plan_service import (
    create_installment_checkout,
    payment_plan_projection,
    release_installment_for_payment,
)
from app.services.service_order_quote_service import resolve_public_order


router = APIRouter(prefix="/payments", tags=["payments"])


class CashPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_order_id: int
    idempotency_key: str | None = None


class PublicStripeCheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The amount is deliberately absent: the server reads the accepted OS snapshot.
    pass


class InstallmentReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_type: str
    observation: str | None = None
    evidence_reference: str | None = None


def _public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


@router.post("/public/service-requests/{tracking_token}/checkout")
def create_public_visit_checkout(
    tracking_token: str,
    _payload: PublicStripeCheckoutRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    if idempotency_key and len(idempotency_key) > 120:
        raise HTTPException(status_code=400, detail="Idempotency-Key invalido")
    if not os.getenv("STRIPE_SECRET_KEY", "").strip():
        raise HTTPException(status_code=503, detail="Checkout Stripe nao configurado")
    service_request = (
        db.query(ServiceRequest)
        .filter(ServiceRequest.tracking_token == tracking_token)
        .first()
    )
    order = service_request.service_order if service_request else None
    if not order or not order.organization_id:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    snapshot = db.query(VisitPricingSnapshot).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).first()
    financial_account = db.query(ServiceOrderFinancial).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).first()
    policy = resolve_payment_policy(db, organization_id=order.organization_id)
    if not snapshot or not financial_account or not policy.visit_required:
        raise HTTPException(status_code=409, detail="La visita no esta disponible para pago")
    if (order.status or "").strip().upper() in {"CANCELLED", "CANCELADA", "CONCLUIDA", "FINALIZADA", "COMPLETED"}:
        raise HTTPException(status_code=409, detail="La orden no admite pago de visita")
    amount = Decimal(snapshot.total_amount)
    if amount <= 0:
        raise HTTPException(status_code=409, detail="La visita aun no tiene un importe definido")
    if str(snapshot.currency).upper() != str(policy.currency).upper():
        raise HTTPException(status_code=409, detail="La moneda de la visita no coincide con la politica")
    existing = db.query(Payment).filter(
        Payment.service_order_id == order.id,
        Payment.organization_id == order.organization_id,
        Payment.payment_type == "TECHNICAL_VISIT",
        Payment.status.in_(["PENDING", "CHECKOUT_CREATED", "PAID"]),
    ).order_by(Payment.created_at.desc(), Payment.id.desc()).first()
    if existing and existing.status == "PAID":
        raise HTTPException(status_code=409, detail="VISIT_ALREADY_PAID")
    payment = existing or create_payment(
        db,
        organization_id=order.organization_id,
        payment_type="TECHNICAL_VISIT",
        payment_method="STRIPE_CARD",
        amount=amount,
        service_request_id=service_request.id,
        service_order_id=order.id,
        lead_id=order.lead_id,
        technician_id=order.responsible_user_id,
        idempotency_key=f"visit-checkout:{order.id}",
    )
    if payment.gross_amount != amount or str(payment.currency).upper() != str(snapshot.currency).upper():
        raise HTTPException(status_code=409, detail="El pago pendiente no coincide con el snapshot")
    base_url = _public_base_url()
    if not base_url:
        raise HTTPException(status_code=503, detail="Checkout publico no configurado")
    create_stripe_checkout(
        db,
        payment,
        success_url=f"{base_url}/seguimiento/{tracking_token}?payment=success",
        cancel_url=f"{base_url}/seguimiento/{tracking_token}?payment=cancelled",
        description="Total Solutions - visita tecnica",
    )
    payment.status = "PENDING"
    db.commit()
    return {"status": payment.status, "checkout_url": payment.checkout_url}


@router.post("/public/service-requests/{tracking_token}/installments/{installment_sequence}/checkout")
def create_public_installment_checkout(
    tracking_token: str,
    installment_sequence: int,
    db: Session = Depends(get_db),
):
    order = resolve_public_order(db, tracking_token)
    payment = create_installment_checkout(db, order, installment_sequence, tracking_token=tracking_token)
    db.commit()
    return {"status": payment.status, "checkout_url": payment.checkout_url, "installment_sequence": installment_sequence}


@router.post("/service-orders/{order_id}/payment-plan/installments/{installment_sequence}/release")
def release_service_order_installment(
    order_id: int,
    installment_sequence: int,
    payload: InstallmentReleaseRequest,
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    query = db.query(ServiceOrder).filter(ServiceOrder.id == order_id)
    if actor.role != "ROOT":
        query = query.filter(ServiceOrder.organization_id == actor.organization_id)
    order = query.first()
    if not order:
        raise HTTPException(status_code=404, detail="Ordem de serviço não encontrada")
    result = release_installment_for_payment(
        db, order, installment_sequence, actor,
        trigger_type=payload.trigger_type.strip().upper(),
        observation=payload.observation,
        evidence_reference=payload.evidence_reference,
    )
    db.commit()
    return result


@router.get("/service-orders/{order_id}/payment-plan")
def get_service_order_payment_plan(
    order_id: int,
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    query = db.query(ServiceOrder).filter(ServiceOrder.id == order_id)
    if actor.role != "ROOT":
        query = query.filter(ServiceOrder.organization_id == actor.organization_id)
    order = query.first()
    if not order:
        raise HTTPException(status_code=404, detail="Ordem de serviço não encontrada")
    projection = payment_plan_projection(db, order)
    if projection is None:
        raise HTTPException(status_code=404, detail="Plano de pagamento não encontrado")
    return projection


@router.post("/service-orders/{order_id}/payment-plan/installments/{installment_sequence}/reconcile")
def reconcile_service_order_installment(
    order_id: int,
    installment_sequence: int,
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    query = db.query(ServiceOrder).filter(ServiceOrder.id == order_id)
    if actor.role != "ROOT":
        query = query.filter(ServiceOrder.organization_id == actor.organization_id)
    order = query.first()
    if not order:
        raise HTTPException(status_code=404, detail="Ordem de serviço não encontrada")
    installment = db.query(ServiceOrderPaymentInstallment).filter_by(
        service_order_id=order.id, organization_id=order.organization_id, sequence=installment_sequence,
    ).first()
    payment = db.query(Payment).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
        installment_id=installment.id if installment else None,
    ).first() if installment else None
    if not payment:
        raise HTTPException(status_code=404, detail="Pagamento da parcela não encontrado")
    try:
        payment = reconcile_stripe_checkout_payment(db, payment)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"payment_id": payment.id, "status": payment.status, "installment_sequence": installment_sequence}


@router.get("")
def list_payments(
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    query = db.query(Payment)
    if actor.role != "ROOT":
        query = query.filter(Payment.organization_id == actor.organization_id)
    return [
        {
            "id": payment.id,
            "organization_id": payment.organization_id,
            "service_order_id": payment.service_order_id,
            "upgrade_intent_id": payment.upgrade_intent_id,
            "payment_type": payment.payment_type,
            "payment_method": payment.payment_method,
            "currency": payment.currency,
            "gross_amount": float(payment.gross_amount),
            "platform_fee_amount": float(payment.platform_fee_amount),
            "status": payment.status,
            "provider": payment.provider,
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            "created_at": payment.created_at.isoformat() if payment.created_at else None,
        }
        for payment in query.order_by(Payment.created_at.desc()).limit(200).all()
    ]


@router.post("/cash")
def record_cash(
    payload: CashPaymentRequest,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    if actor.role != "BROKER":
        raise HTTPException(status_code=403, detail="Somente o tecnico responsavel pode registrar efectivo")
    order = (
        db.query(ServiceOrder)
        .filter(ServiceOrder.id == payload.service_order_id, ServiceOrder.organization_id == actor.organization_id)
        .first()
    )
    if not order or order.responsible_user_id != actor.id:
        raise HTTPException(status_code=404, detail="Ordem de serviço não encontrada")
    amount = order.final_service_price or order.visit_calculated_price
    if amount is None or Decimal(amount) <= 0:
        raise HTTPException(status_code=409, detail="A OS ainda nao possui valor de recebimento definido")
    payment = create_payment(
        db,
        organization_id=actor.organization_id,
        payment_type="TECHNICAL_VISIT" if order.final_service_price is None else "SERVICE",
        payment_method="CASH",
        amount=Decimal(amount),
        service_order_id=order.id,
        lead_id=order.lead_id,
        technician_id=actor.id,
        idempotency_key=payload.idempotency_key or f"cash:service-order:{order.id}",
    )
    record_cash_payment(db, payment)
    db.commit()
    return {"payment_id": payment.id, "status": payment.status, "amount": float(payment.gross_amount), "platform_fee": float(payment.platform_fee_amount)}


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    if not verify_stripe_signature(body, request.headers.get("stripe-signature")):
        raise HTTPException(status_code=400, detail="Assinatura Stripe invalida")
    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Payload Stripe invalido") from exc
    payment = handle_stripe_event(db, event)
    if payment and payment.status == "PAID" and payment.payment_type == "SUBSCRIPTION_PLAN":
        activate_upgrade_from_paid_payment(db, payment, request=request)
    db.commit()
    return {"received": True}
