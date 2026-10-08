from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db, require_admin_user, get_current_user
from app.core.security import verify_password
from app.models.customer_invitation import CustomerPortalInvitation
from app.models.user import User
from app.services.customer_account_service import customer_invitation_config
from app.services.customer_invitation_service import (
    activate_new_customer,
    consume_existing_customer_invitation,
    create_invitation,
    inspect_token,
    resend_invitation,
    revoke_invitation,
)

router = APIRouter(prefix="/customer-portal", tags=["customer-portal-invitations"])


class InvitationCreate(BaseModel):
    service_request_id: int
    channel: str
    language: str = "es"
    idempotency_key: str = Field(min_length=8, max_length=160)


class ActivationInput(BaseModel):
    token: str = Field(min_length=20)
    full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=10, max_length=256)
    email: str | None = None
    phone: str | None = None


def _response(invitation):
    return {"status": invitation.status, "expires_at": invitation.expires_at, "delivery_channel": invitation.channel, "masked_destination": invitation.masked_destination}


@router.post("/admin/invitations")
def create_admin_invitation(payload: InvitationCreate, actor: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    invitation, _raw = create_invitation(db, actor=actor, service_request_id=payload.service_request_id, channel=payload.channel.upper(), language=payload.language, idempotency_key=payload.idempotency_key)
    db.commit()
    return _response(invitation)


@router.post("/admin/invitations/{invitation_id}/revoke")
def revoke_admin_invitation(invitation_id: int, actor: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    invitation = db.query(CustomerPortalInvitation).filter(CustomerPortalInvitation.id == invitation_id).first()
    if not invitation:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Convite não encontrado")
    revoke_invitation(db, invitation, actor)
    db.commit()
    return _response(invitation)


@router.post("/admin/invitations/{invitation_id}/resend")
def resend_admin_invitation(invitation_id: int, idempotency_key: str, actor: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    invitation = db.query(CustomerPortalInvitation).filter(CustomerPortalInvitation.id == invitation_id).first()
    if not invitation:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Convite não encontrado")
    replacement, _raw = resend_invitation(db, invitation, actor, idempotency_key)
    db.commit()
    return _response(replacement)


@router.get("/activation")
def activation_state(token: str, db: Session = Depends(get_db)):
    invitation = inspect_token(db, token)
    if not invitation:
        return {"status": "INVALID"}
    return {"status": "VALID", "delivery_channel": invitation.channel, "expires_at": invitation.expires_at}


@router.post("/activation")
def activate_customer(payload: ActivationInput, db: Session = Depends(get_db)):
    user = activate_new_customer(db, payload.token, payload.full_name, payload.password, payload.email, payload.phone)
    db.commit()
    return {"status": "ACTIVATED", "redirect": "/cliente", "role": user.role}


@router.post("/claims/verify-existing")
def verify_existing_claim(token: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.customer_account_service import require_customer
    require_customer(user, db)
    consume_existing_customer_invitation(db, token, user)
    db.commit()
    return {"status": "VERIFIED", "redirect": "/cliente"}
