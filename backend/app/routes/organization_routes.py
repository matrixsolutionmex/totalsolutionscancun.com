from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db, require_admin_user, require_root_user
from app.models.organization import Organization
from app.models.organization_invitation import OrganizationInvitation
from app.models.user import User
from app.services.organization_onboarding_service import accept_invitation, create_invitation, invitation_for_token
from app.services.notification_service import enqueue_invitation_email
from app.services.entitlement_service import current_plan

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
    if actor.role == "GERENTE":
        if current_plan(db, actor) not in {"PRO", "BUSINESS"}:
            raise HTTPException(status_code=403, detail="Convites de equipe exigem entitlement PRO ou BUSINESS")
        if payload.role.strip().upper() != "BROKER":
            raise HTTPException(status_code=403, detail="Supervisor pode convidar apenas técnicos")
        payload = payload.model_copy(update={"role": "BROKER", "supervisor_user_id": actor.id})
    invitation, _raw_token = create_invitation(
        db,
        organization=organization,
        invited_by=actor,
        invited_email=str(payload.invited_email),
        role=payload.role,
        supervisor_user_id=payload.supervisor_user_id,
    )
    enqueue_invitation_email(db, invitation=invitation, organization_name=organization.name)
    db.commit()
    return {
        **invitation_payload(invitation, organization),
        "email_delivery_status": "queued",
    }


@router.get("/team")
def current_team(
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    if actor.role == "GERENTE":
        if current_plan(db, actor) not in {"PRO", "BUSINESS"}:
            raise HTTPException(status_code=403, detail="Acesso à equipe exige entitlement PRO ou BUSINESS")
        users = db.query(User).filter(User.organization_id == actor.organization_id, User.manager_id == actor.id, User.role == "BROKER").order_by(User.full_name, User.id).all()
    else:
        users = db.query(User).filter(User.organization_id == actor.organization_id, User.role == "BROKER").order_by(User.full_name, User.id).all()
    return [{
        "id": user.id,
        "full_name": user.full_name or user.username,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "organization_id": user.organization_id,
        "supervisor_id": user.manager_id,
        "plan": current_plan(db, user),
    } for user in users]


@router.post("/team/invitations")
def create_team_invitation(
    payload: InvitationCreateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    if actor.role == "GERENTE":
        if current_plan(db, actor) not in {"PRO", "BUSINESS"}:
            raise HTTPException(status_code=403, detail="Convites de equipe exigem entitlement PRO ou BUSINESS")
        supervisor_user_id = actor.id
    else:
        supervisor_user_id = payload.supervisor_user_id
    if not supervisor_user_id:
        raise HTTPException(status_code=400, detail="Supervisor obrigatório")
    supervisor_query = db.query(User).filter(User.id == supervisor_user_id, User.role == "GERENTE", User.is_active.is_(True), User.status == "ACTIVE")
    if actor.role == "GERENTE":
        supervisor_query = supervisor_query.filter(User.organization_id == actor.organization_id)
    supervisor = supervisor_query.first()
    if not supervisor:
        raise HTTPException(status_code=400, detail="Supervisor fora da organização ou inválido")
    if current_plan(db, supervisor) not in {"PRO", "BUSINESS"}:
        raise HTTPException(status_code=403, detail="Convites de equipe exigem entitlement PRO ou BUSINESS")
    organization = db.query(Organization).filter(Organization.id == supervisor.organization_id).first()
    invitation, _raw_token = create_invitation(db, organization=organization, invited_by=actor, invited_email=payload.invited_email, role="BROKER", supervisor_user_id=supervisor.id)
    enqueue_invitation_email(db, invitation=invitation, organization_name=organization.name)
    db.commit()
    return {**invitation_payload(invitation, organization), "email_delivery_status": "queued"}


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
