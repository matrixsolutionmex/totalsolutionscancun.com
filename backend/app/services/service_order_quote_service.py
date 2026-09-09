from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.service_order import ServiceOrder
from app.models.service_order_diagnosis import ServiceOrderDiagnosis
from app.models.service_order_payment_plan import ServiceOrderPaymentPlan
from app.models.service_order_quote import ServiceOrderQuote, ServiceOrderQuoteItem
from app.models.service_request import ServiceRequest

QUOTE_VISIBLE_STATUSES = {"SENT", "APPROVED", "REJECTED"}
ACTIVE_QUOTE_STATUSES = {"DRAFT", "SENT"}
MONEY = Decimal("0.01")


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Valor do orçamento inválido") from exc


def _actor_can_access_order(order: ServiceOrder, actor) -> bool:
    if actor.role in {"ROOT", "GERENTE"}:
        return True
    if actor.id in {order.responsible_user_id, order.supervisor_user_id}:
        return True
    responsible = order.responsible_user
    return bool(responsible and responsible.manager_id == actor.id)


def scoped_order(db: Session, order_id: int, actor) -> ServiceOrder:
    order = db.query(ServiceOrder).filter(ServiceOrder.id == order_id).first()
    if not order or order.organization_id != actor.organization_id or not _actor_can_access_order(order, actor):
        raise HTTPException(status_code=404, detail="Orden de servicio no encontrada")
    return order


def diagnosis_payload(diagnosis: ServiceOrderDiagnosis | None) -> dict[str, Any] | None:
    if not diagnosis:
        return None
    return {
        "diagnosis": diagnosis.diagnosis_text,
        "problem_found": diagnosis.problem_found,
        "recommended_solution": diagnosis.recommended_solution,
        "observations": diagnosis.observations,
        "created_at": diagnosis.created_at,
        "updated_at": diagnosis.updated_at,
    }


def upsert_diagnosis(db: Session, order: ServiceOrder, actor, payload: dict[str, Any]) -> ServiceOrderDiagnosis:
    diagnosis = db.query(ServiceOrderDiagnosis).filter_by(service_order_id=order.id, organization_id=order.organization_id).first()
    if diagnosis is None:
        diagnosis = ServiceOrderDiagnosis(service_order_id=order.id, organization_id=order.organization_id, responsible_user_id=actor.id)
        db.add(diagnosis)
    diagnosis.diagnosis_text = (payload.get("diagnosis") or payload.get("diagnosis_text") or "").strip()
    diagnosis.problem_found = payload.get("problem_found")
    diagnosis.recommended_solution = payload.get("recommended_solution")
    diagnosis.observations = payload.get("observations")
    diagnosis.responsible_user_id = actor.id
    if not diagnosis.diagnosis_text and not diagnosis.problem_found and not diagnosis.recommended_solution:
        raise HTTPException(status_code=422, detail="Informe o diagnóstico técnico")
    return diagnosis


def quote_payload(quote: ServiceOrderQuote, public: bool = False) -> dict[str, Any]:
    result = {
        "version": quote.version,
        "status": quote.status,
        "subtotal": quote.subtotal,
        "discount_amount": quote.discount_amount,
        "tax_amount": quote.tax_amount,
        "total": quote.total,
        "currency": quote.currency,
        "valid_until": quote.valid_until,
        "notes": quote.notes,
        "items": [{"description": item.description, "quantity": item.quantity, "unit": item.unit, "unit_price": item.unit_price, "subtotal": item.subtotal} for item in quote.items],
    }
    if not public:
        result.update({"id": quote.id, "service_order_id": quote.service_order_id, "organization_id": quote.organization_id, "created_by_user_id": quote.created_by_user_id, "created_at": quote.created_at, "approved_at": quote.approved_at, "rejection_reason": quote.rejection_reason})
    else:
        result.update({"approved_at": quote.approved_at, "rejection_reason": quote.rejection_reason})
    return result


def create_quote(db: Session, order: ServiceOrder, actor, payload: dict[str, Any]) -> ServiceOrderQuote:
    raw_items = payload.get("items") or []
    if not raw_items:
        raise HTTPException(status_code=422, detail="O orçamento precisa de pelo menos um item")
    max_version = db.query(func.max(ServiceOrderQuote.version)).filter_by(service_order_id=order.id, organization_id=order.organization_id).scalar() or 0
    previous_quotes = db.query(ServiceOrderQuote).filter(
        ServiceOrderQuote.service_order_id == order.id,
        ServiceOrderQuote.organization_id == order.organization_id,
        ServiceOrderQuote.status.in_(ACTIVE_QUOTE_STATUSES | {"APPROVED"}),
    ).all()
    for previous in previous_quotes:
        previous_plan = db.query(ServiceOrderPaymentPlan).filter_by(
            service_order_id=order.id,
            organization_id=order.organization_id,
            quote_id=previous.id,
            quote_version=previous.version,
            status="ACTIVE",
        ).first()
        if previous_plan and any(item.status == "PAID" for item in previous_plan.installments):
            raise HTTPException(status_code=409, detail="Orçamento com pagamento não pode ser substituído")
        previous.status = "SUPERSEDED"
        if previous_plan:
            previous_plan.status = "SUPERSEDED"
    items = []
    subtotal = Decimal("0.00")
    for index, raw in enumerate(raw_items):
        quantity = Decimal(str(raw.get("quantity", 0)))
        unit_price = _money(raw.get("unit_price"))
        if quantity <= 0 or unit_price < 0:
            raise HTTPException(status_code=422, detail="Quantidade e preço do item inválidos")
        item_subtotal = (quantity * unit_price).quantize(MONEY, rounding=ROUND_HALF_UP)
        subtotal += item_subtotal
        items.append(ServiceOrderQuoteItem(organization_id=order.organization_id, description=str(raw.get("description", "")).strip(), quantity=quantity, unit=str(raw.get("unit") or "unidad"), unit_price=unit_price, subtotal=item_subtotal, sort_order=index))
    if any(not item.description for item in items):
        raise HTTPException(status_code=422, detail="Todo item precisa de descrição")
    discount = _money(payload.get("discount_amount"))
    tax = _money(payload.get("tax_amount"))
    total = (subtotal - discount + tax).quantize(MONEY, rounding=ROUND_HALF_UP)
    if discount < 0 or tax < 0 or total < 0:
        raise HTTPException(status_code=422, detail="Totais do orçamento inválidos")
    quote = ServiceOrderQuote(service_order_id=order.id, organization_id=order.organization_id, version=max_version + 1, status="DRAFT", subtotal=subtotal, discount_amount=discount, tax_amount=tax, total=total, currency=str(payload.get("currency") or order.pricing_currency or "MXN").upper(), valid_until=payload.get("valid_until"), notes=payload.get("notes"), created_by_user_id=actor.id, items=items)
    db.add(quote)
    return quote


def latest_public_quote(db: Session, order_id: int, organization_id: int) -> ServiceOrderQuote | None:
    return db.query(ServiceOrderQuote).filter(ServiceOrderQuote.service_order_id == order_id, ServiceOrderQuote.organization_id == organization_id, ServiceOrderQuote.status.in_(QUOTE_VISIBLE_STATUSES)).order_by(ServiceOrderQuote.version.desc()).first()


def public_quote_projection(db: Session, order: ServiceOrder) -> dict[str, Any] | None:
    try:
        quote = latest_public_quote(db, order.id, order.organization_id)
        diagnosis = db.query(ServiceOrderDiagnosis).filter_by(service_order_id=order.id, organization_id=order.organization_id).first()
    except OperationalError:
        # Older test/clone schemas can omit the optional 071 tables until startup create_all runs.
        db.rollback()
        return None
    if not quote and not diagnosis:
        return None
    return {"diagnosis": diagnosis_payload(diagnosis), "quote": quote_payload(quote, public=True) if quote else None}


def resolve_public_order(db: Session, token: str) -> ServiceOrder:
    request = db.query(ServiceRequest).filter(ServiceRequest.tracking_token == token).first()
    if not request or not request.service_order:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    return request.service_order


def current_tracking_token_for_order(db: Session, order: ServiceOrder) -> str:
    """Return the persisted public token for an order's own service request."""
    if not order or not order.service_request_id:
        raise HTTPException(status_code=409, detail="A OS não possui uma solicitação pública válida")
    request = db.query(ServiceRequest).filter(
        ServiceRequest.id == order.service_request_id,
        ServiceRequest.organization_id == order.organization_id,
    ).first()
    token = (request.tracking_token or "").strip() if request else ""
    if not token:
        raise HTTPException(status_code=409, detail="A OS não possui um token público válido")
    return token


def approve_public_quote(db: Session, order: ServiceOrder) -> ServiceOrderQuote:
    quote = latest_public_quote(db, order.id, order.organization_id)
    if not quote or quote.status != "SENT":
        raise HTTPException(status_code=409, detail="Orçamento não está disponível para aprovação")
    if quote.valid_until and quote.valid_until <= datetime.utcnow():
        quote.status = "EXPIRED"
        raise HTTPException(status_code=409, detail="Orçamento expirado")
    quote.status = "APPROVED"
    quote.approved_at = datetime.utcnow()
    quote.approved_source = "PUBLIC_TRACKING_TOKEN"
    quote.approved_total = quote.total
    quote.approved_snapshot = {"version": quote.version, "currency": quote.currency, "subtotal": str(quote.subtotal), "discount_amount": str(quote.discount_amount), "tax_amount": str(quote.tax_amount), "total": str(quote.total), "items": [{"description": i.description, "quantity": str(i.quantity), "unit": i.unit, "unit_price": str(i.unit_price), "subtotal": str(i.subtotal)} for i in quote.items]}
    from app.services.service_order_payment_plan_service import create_payment_plan_for_approved_quote
    create_payment_plan_for_approved_quote(db, order, quote)
    return quote


def reject_public_quote(db: Session, order: ServiceOrder, reason: str | None) -> ServiceOrderQuote:
    quote = latest_public_quote(db, order.id, order.organization_id)
    if not quote or quote.status != "SENT":
        raise HTTPException(status_code=409, detail="Orçamento não está disponível para recusa")
    quote.status = "REJECTED"
    quote.rejected_at = datetime.utcnow()
    quote.rejection_reason = (reason or "").strip()[:1000] or None
    return quote
