from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.services.service_order_quote_service import create_quote, diagnosis_payload, quote_payload, scoped_order, upsert_diagnosis

router = APIRouter(tags=["service-order-quotes"])


class DiagnosisInput(BaseModel):
    diagnosis: str | None = None
    problem_found: str | None = None
    recommended_solution: str | None = None
    observations: str | None = None


class QuoteItemInput(BaseModel):
    description: str = Field(min_length=1, max_length=240)
    quantity: Decimal = Field(gt=0)
    unit: str = "unidad"
    unit_price: Decimal = Field(ge=0)


class QuoteInput(BaseModel):
    items: list[QuoteItemInput] = Field(min_length=1)
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    tax_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    currency: str = Field(default="MXN", min_length=3, max_length=8)
    valid_until: datetime | None = None
    notes: str | None = None


@router.post("/service-orders/{order_id}/diagnosis")
def create_diagnosis(order_id: int, payload: DiagnosisInput, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = scoped_order(db, order_id, actor)
    diagnosis = upsert_diagnosis(db, order, actor, payload.model_dump())
    db.commit()
    db.refresh(diagnosis)
    return diagnosis_payload(diagnosis)


@router.post("/service-orders/{order_id}/quotes", status_code=201)
def create_service_order_quote(order_id: int, payload: QuoteInput, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = scoped_order(db, order_id, actor)
    quote = create_quote(db, order, actor, payload.model_dump())
    db.commit()
    db.refresh(quote)
    return quote_payload(quote)


@router.get("/service-orders/{order_id}/quotes")
def list_service_order_quotes(order_id: int, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = scoped_order(db, order_id, actor)
    from app.models.service_order_quote import ServiceOrderQuote
    return [quote_payload(row) for row in db.query(ServiceOrderQuote).filter_by(service_order_id=order.id, organization_id=order.organization_id).order_by(ServiceOrderQuote.version.desc()).all()]


@router.post("/service-orders/{order_id}/quotes/{quote_id}/send")
def send_service_order_quote(order_id: int, quote_id: int, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = scoped_order(db, order_id, actor)
    from fastapi import HTTPException
    from app.models.service_order_quote import ServiceOrderQuote
    quote = db.query(ServiceOrderQuote).filter_by(id=quote_id, service_order_id=order.id, organization_id=order.organization_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Orçamento não encontrado")
    if quote.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Somente rascunhos podem ser enviados")
    quote.status = "SENT"
    db.commit()
    db.refresh(quote)
    return quote_payload(quote)
