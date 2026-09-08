from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.services.service_order_review_service import get_scoped_review, quality_projection

router = APIRouter(tags=["service-order-quality"])


class ReviewInput(BaseModel):
    overall_rating: int = Field(ge=1, le=5)
    service_quality_rating: int = Field(ge=1, le=5)
    punctuality_rating: int = Field(ge=1, le=5)
    communication_rating: int = Field(ge=1, le=5)
    nps_score: int | None = Field(default=None, ge=0, le=10)
    comment: str | None = Field(default=None, max_length=2000)


@router.get("/service-orders/{order_id}/review")
def service_order_review(order_id: int, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    return get_scoped_review(db, order_id, actor)


@router.get("/organization/quality")
def organization_quality(actor=Depends(get_current_user), db: Session = Depends(get_db)):
    if actor.role not in {"ROOT", "GERENTE", "SUPERVISOR", "BROKER", "TECNICO", "TÉCNICO"}:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Sem permissão para consultar qualidade")
    return quality_projection(db, actor)
