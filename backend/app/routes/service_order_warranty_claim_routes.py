from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.services.service_order_quote_service import scoped_order
from app.services.service_order_warranty_claim_service import (
    assign_claim, claim_payload, claims_projection, create_claim, review_claim, update_claim,
)

router = APIRouter(tags=["service-order-warranty"])


class ClaimInput(BaseModel):
    reason: str = Field(min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=4000)
    evidence_reference: str | None = Field(default=None, max_length=240)
    idempotency_key: str = Field(min_length=1, max_length=128)


class ReviewInput(BaseModel):
    approve: bool
    notes: str | None = Field(default=None, max_length=4000)


class AssignInput(BaseModel):
    technician_id: int


class UpdateInput(BaseModel):
    status: str
    notes: str | None = Field(default=None, max_length=4000)


@router.post("/service-orders/{order_id}/warranty-claims", status_code=201)
def create_warranty_claim(order_id: int, payload: ClaimInput, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = scoped_order(db, order_id, actor)
    row = create_claim(db, order, **payload.model_dump(), actor=actor, source="INTERNAL")
    db.commit(); db.refresh(row)
    return claim_payload(row)


@router.get("/service-orders/{order_id}/warranty-claims")
def list_warranty_claims(order_id: int, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = scoped_order(db, order_id, actor)
    return claims_projection(db, order)


@router.post("/warranty-claims/{claim_id}/review")
def review_warranty_claim(claim_id: int, payload: ReviewInput, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    row = review_claim(db, claim_id, actor, **payload.model_dump())
    db.commit(); db.refresh(row)
    return claim_payload(row)


@router.post("/warranty-claims/{claim_id}/assign")
def assign_warranty_claim(claim_id: int, payload: AssignInput, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    row = assign_claim(db, claim_id, actor, payload.technician_id)
    db.commit(); db.refresh(row)
    return claim_payload(row)


@router.post("/warranty-claims/{claim_id}/update")
def update_warranty_claim(claim_id: int, payload: UpdateInput, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    row = update_claim(db, claim_id, actor, **payload.model_dump())
    db.commit(); db.refresh(row)
    return claim_payload(row)
