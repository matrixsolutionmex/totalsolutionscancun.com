from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db, require_admin_user
from app.models.user import User
from app.services.technician_recommendation_service import recommend_technicians

router = APIRouter(tags=["technician-recommendations"])


@router.get("/service-orders/{order_id}/technician-recommendations")
def technician_recommendations(order_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    return recommend_technicians(db, order_id, actor)

