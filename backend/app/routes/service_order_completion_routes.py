from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.services.service_order_completion_service import (
    completion_projection,
    record_technical_completion,
    review_technical_completion,
    _assert_completion_access,
)


router = APIRouter(tags=["service-order-completion"])


class TechnicalCompletionInput(BaseModel):
    completion_notes: str | None = Field(default=None, max_length=4000)
    final_observation: str | None = Field(default=None, max_length=4000)
    evidence_reference: str | None = Field(default=None, max_length=240)


@router.post("/service-orders/{order_id}/technical-completion")
def create_technical_completion(order_id: int, payload: TechnicalCompletionInput, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = _assert_completion_access(db, order_id, actor)
    row = record_technical_completion(db, order, actor, payload.model_dump())
    db.commit()
    db.refresh(row)
    return completion_projection(db, order)


@router.post("/service-orders/{order_id}/technical-completion/review")
def review_completion(order_id: int, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = _assert_completion_access(db, order_id, actor)
    review_technical_completion(db, order, actor)
    db.commit()
    return completion_projection(db, order)


@router.get("/service-orders/{order_id}/completion")
def get_completion(order_id: int, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    order = _assert_completion_access(db, order_id, actor)
    return completion_projection(db, order)
