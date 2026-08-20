from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db, require_admin_user
from app.models.service_opportunity import ServiceOpportunity
from app.models.user import User
from app.services.marketplace_service import (
    claim_opportunity,
    assign_opportunity,
    get_available_opportunity,
    list_opportunities,
    private_opportunity,
    public_opportunity,
    seed_demo_opportunities,
    can_access_marketplace,
)
from app.services.pablo_location_service import get_active_location


router = APIRouter(prefix="/marketplace", tags=["marketplace"])


class MarketplaceSeedRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=12)


class MarketplaceAssignmentRequest(BaseModel):
    target_user_id: int = Field(gt=0)


@router.get("/opportunities")
def marketplace_opportunities(
    service: str | None = None,
    segment: str | None = None,
    country: str | None = None,
    state: str | None = None,
    city: str | None = None,
    urgency: str | None = None,
    distance: float | None = Query(default=None, ge=0, le=500),
    sort: str = Query(default="distance", pattern="^(distance|urgency|value|recent)$"),
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    result = list_opportunities(db, actor, service=service or segment, city=city, state=state,
                                country=country, urgency=urgency, max_distance=distance, sort=sort)
    return {"opportunities": result, "location_available": get_active_location(actor) is not None}


@router.get("/opportunities/{public_id}")
def marketplace_opportunity_detail(public_id: str, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    opportunity = get_available_opportunity(db, actor, public_id)
    return {"opportunity": public_opportunity(opportunity)}


@router.post("/opportunities/{public_id}/claim")
def marketplace_opportunity_claim(public_id: str, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    return claim_opportunity(db, actor, public_id)


@router.post("/opportunities/{public_id}/assign")
def marketplace_opportunity_assign(
    public_id: str,
    payload: MarketplaceAssignmentRequest,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    return assign_opportunity(db, actor, public_id, payload.target_user_id)


@router.get("/my-services")
def marketplace_my_services(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    if not can_access_marketplace(db, actor):
        raise HTTPException(status_code=403, detail="Seu perfil não possui acesso ao Marketplace.")
    target_ids = [actor.id]
    if actor.role == "GERENTE":
        target_ids.extend(
            user.id for user in db.query(User).filter(
                User.organization_id == actor.organization_id,
                User.manager_id == actor.id,
                User.role == "BROKER",
                User.is_active.is_(True),
                User.status == "ACTIVE",
            ).all()
        )
    query = db.query(ServiceOpportunity).filter(
        ServiceOpportunity.claimed_by_user_id.in_(target_ids),
        ServiceOpportunity.status.in_(["CLAIMED", "IN_PROGRESS", "COMPLETED"]),
    )
    if actor.role != "ROOT":
        query = query.filter(ServiceOpportunity.organization_id == actor.organization_id)
    opportunities = query.order_by(ServiceOpportunity.claimed_at.desc()).all()
    return {"opportunities": [private_opportunity(db, item, actor) for item in opportunities]}


@router.post("/dev/seed")
def marketplace_seed(payload: MarketplaceSeedRequest, db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    return {"created": seed_demo_opportunities(db, actor, payload.count)}
