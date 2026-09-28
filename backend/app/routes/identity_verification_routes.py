from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.models.user import User
from app.services.identity_verification_service import get_identity_verification_payload


router = APIRouter(prefix="/identity-verification", tags=["identity-verification"])


@router.get("/me")
def get_my_identity_verification(
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    return get_identity_verification_payload(db, actor)
