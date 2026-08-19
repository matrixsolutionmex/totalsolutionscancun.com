import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db, require_admin_user, require_root_user
from app.models.organization import Organization
from app.models.organization_invitation import OrganizationInvitation
from app.models.user import User
from app.services.organization_onboarding_service import accept_invitation, create_invitation, invitation_for_token, send_invitation_email

router = APIRouter(prefix="/organization", tags=["organization"])


class InvitationCreateRequest(BaseModel):
    invited_email: str
    role: str = "BROKER"
    supervisor_user_id: int | None = None


class InvitationAcceptRequest(BaseModel):
    token: str


def invitation_payload(invitation: OrganizationInvitation, organization: Organization) -> dict:
    return {
        "id": invitation.id,
        "organization_id": invitation.organization_id,
        "organization_name": organization.name,
        "invited_email": invitation.invited_email,
        "role": invitation.role,
        "supervisor_user_id": invitation.supervisor_user_id,
        "status": invitation.status,
        "expires_at": invitation.expires_at.isoformat(),
        "accepted_at": invitation.accepted_at.isoformat() if invitation.accepted_at else None,
    }


@router.post("/invitations")
def create_organization_invitation(
    payload: InvitationCreateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    if not actor.organization_id:
        raise HTTPException(status_code=400, detail="Usuário sem organização")
    organization = db.query(Organization).filter(Organization.id == actor.organization_id).first()
    invitation, raw_token = create_invitation(
        db,
        organization=organization,
        invited_by=actor,
        invited_email=str(payload.invited_email),
        role=payload.role,
        supervisor_user_id=payload.supervisor_user_id,
    )
    db.commit()
    base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    invite_url = f"{base_url}/invite/{raw_token}" if base_url else f"/invite/{raw_token}"
    email_sent = send_invitation_email(
        to_email=str(payload.invited_email),
        organization_name=organization.name,
        role=invitation.role,
        invite_url=invite_url,
    )
    return {
        **invitation_payload(invitation, organization),
        "invite_url": invite_url,
        "email_delivery_status": "sent" if email_sent else "unavailable",
    }


@router.get("/available")
def available_organizations(
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return [
        {"id": organization.id, "name": organization.name, "plan": organization.plan, "status": organization.status}
        for organization in db.query(Organization).order_by(Organization.name).all()
    ]


@router.get("/invitations/{token}")
def preview_organization_invitation(token: str, db: Session = Depends(get_db)):
    invitation = invitation_for_token(db, token)
    organization = db.query(Organization).filter(Organization.id == invitation.organization_id).first()
    return invitation_payload(invitation, organization)


@router.post("/invitations/accept")
def accept_organization_invitation(
    payload: InvitationAcceptRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    invitation = accept_invitation(db, raw_token=payload.token, user=actor)
    db.commit()
    return {"accepted": True, "organization_id": invitation.organization_id, "role": invitation.role}


@router.post("/invitations/{invitation_id}/cancel")
def cancel_organization_invitation(
    invitation_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    invitation = db.query(OrganizationInvitation).filter(
        OrganizationInvitation.id == invitation_id,
        OrganizationInvitation.organization_id == actor.organization_id,
        OrganizationInvitation.status == "PENDING",
    ).first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Convite pendente não encontrado")
    invitation.status = "CANCELLED"
    db.commit()
    return {"cancelled": True, "id": invitation.id}
