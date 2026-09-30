from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db, require_root_user
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.technician_transfer import TRANSFER_TYPES, TechnicianTransferRequest
from app.models.user import User
from app.services.organization_membership_service import (
    approve_exclusive_transfer,
    get_active_memberships,
    membership_schema_available,
    transfer_blockers,
)


router = APIRouter(tags=["organization-memberships"])


class TransferRequestInput(BaseModel):
    to_organization_id: int
    requested_transfer_type: str = "NETWORK_MEMBERSHIP"
    reason: str | None = Field(default=None, max_length=2000)
    terms_version: str | None = Field(default=None, max_length=64)
    accept_terms: bool = False


class ReviewInput(BaseModel):
    review_notes: str | None = Field(default=None, max_length=2000)


def require_network_schema(db: Session) -> None:
    if not membership_schema_available(db):
        raise HTTPException(status_code=503, detail="Membership operacional indisponível")


def membership_payload(row: OrganizationMembership) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "organization_id": row.organization_id,
        "membership_type": row.membership_type,
        "role": row.role,
        "status": row.status,
        "supervisor_user_id": row.supervisor_user_id,
        "is_primary": row.is_primary,
        "is_operational": row.is_operational,
        "joined_at": row.joined_at.isoformat() if row.joined_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "terms_version": row.terms_version,
        "terms_accepted_at": row.terms_accepted_at.isoformat() if row.terms_accepted_at else None,
        "territory_country": row.territory_country,
        "territory_state": row.territory_state,
        "territory_city": row.territory_city,
    }


def transfer_payload(row: TechnicianTransferRequest, blockers: list[dict] | None = None) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "from_organization_id": row.from_organization_id,
        "to_organization_id": row.to_organization_id,
        "status": row.status,
        "requested_transfer_type": row.requested_transfer_type,
        "reason": row.reason,
        "review_notes": row.review_notes,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "terms_version": row.terms_version,
        "terms_accepted_at": row.terms_accepted_at.isoformat() if row.terms_accepted_at else None,
        "blockers": blockers or [],
    }


@router.get("/me/memberships")
def my_memberships(db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    require_network_schema(db)
    return {"items": [membership_payload(row) for row in get_active_memberships(db, actor.id)]}


@router.get("/network/organizations")
def network_organizations(db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    """Return safe organization choices without exposing operational or billing data."""
    require_network_schema(db)
    rows = db.query(Organization).filter(Organization.status == "ACTIVE").order_by(Organization.name.asc()).all()
    return [{"id": row.id, "name": row.name, "slug": row.slug} for row in rows if row.id != actor.organization_id]


@router.post("/me/transfer-requests", status_code=201)
def create_transfer_request(payload: TransferRequestInput, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    require_network_schema(db)
    transfer_type = payload.requested_transfer_type.strip().upper()
    if transfer_type not in TRANSFER_TYPES:
        raise HTTPException(status_code=422, detail="Tipo de transferência inválido")
    if not actor.organization_id or payload.to_organization_id == actor.organization_id:
        raise HTTPException(status_code=400, detail="Organização de destino inválida")
    target = db.query(Organization).filter(Organization.id == payload.to_organization_id, Organization.status == "ACTIVE").first()
    if not target:
        raise HTTPException(status_code=404, detail="Organização de destino não encontrada")
    if not get_active_memberships(db, actor.id):
        raise HTTPException(status_code=503, detail="Membership operacional indisponível")
    pending = db.query(TechnicianTransferRequest).filter(
        TechnicianTransferRequest.user_id == actor.id,
        TechnicianTransferRequest.status.in_({"TRANSFER_REQUESTED", "TRANSFER_PENDING_BLOCKED"}),
    ).first()
    if pending:
        return transfer_payload(pending, transfer_blockers(db, actor.id, pending.from_organization_id))
    now = datetime.utcnow()
    row = TechnicianTransferRequest(
        user_id=actor.id,
        from_organization_id=actor.organization_id,
        to_organization_id=payload.to_organization_id,
        requested_transfer_type=transfer_type,
        reason=payload.reason,
        terms_version=payload.terms_version,
        terms_accepted_at=now if payload.accept_terms and payload.terms_version else None,
        status="TRANSFER_REQUESTED",
    )
    db.add(row)
    db.commit()
    return transfer_payload(row, transfer_blockers(db, actor.id, actor.organization_id) if transfer_type == "EXCLUSIVE_TRANSFER" else [])


@router.get("/me/transfer-requests")
def my_transfer_requests(db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    require_network_schema(db)
    rows = db.query(TechnicianTransferRequest).filter(TechnicianTransferRequest.user_id == actor.id).order_by(TechnicianTransferRequest.created_at.desc()).all()
    return {"items": [transfer_payload(row, transfer_blockers(db, actor.id, row.from_organization_id) if row.requested_transfer_type == "EXCLUSIVE_TRANSFER" else []) for row in rows]}


@router.post("/network/transfer-requests/{request_id}/approve")
def approve_transfer(request_id: int, payload: ReviewInput, db: Session = Depends(get_db), actor: User = Depends(require_root_user)):
    require_network_schema(db)
    row = db.query(TechnicianTransferRequest).filter(TechnicianTransferRequest.id == request_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    if row.status not in {"TRANSFER_REQUESTED", "TRANSFER_PENDING_BLOCKED"}:
        raise HTTPException(status_code=409, detail="Solicitação não está pendente")
    if row.requested_transfer_type == "NETWORK_MEMBERSHIP":
        target = db.query(Organization).filter(Organization.id == row.to_organization_id, Organization.status == "ACTIVE").first()
        if not target:
            raise HTTPException(status_code=404, detail="Organização de destino não encontrada")
        membership = db.query(OrganizationMembership).filter_by(user_id=row.user_id, organization_id=row.to_organization_id).first()
        if not membership:
            membership = OrganizationMembership(user_id=row.user_id, organization_id=row.to_organization_id, membership_type="NETWORK_PARTNER", role="NETWORK_PARTNER", status="ACTIVE", is_primary=False, is_operational=True, joined_at=datetime.utcnow(), approved_at=datetime.utcnow(), approved_by_user_id=actor.id, terms_version=row.terms_version, terms_accepted_at=row.terms_accepted_at)
            db.add(membership)
        else:
            membership.status = "ACTIVE"
            membership.is_operational = True
            membership.membership_type = "NETWORK_PARTNER"
            membership.role = "NETWORK_PARTNER"
            membership.terms_version = row.terms_version
            membership.terms_accepted_at = row.terms_accepted_at
        row.status = "APPROVED"
        row.reviewed_at = datetime.utcnow()
        row.reviewed_by_user_id = actor.id
        row.review_notes = payload.review_notes
        db.commit()
        return {"request": transfer_payload(row), "membership": membership_payload(membership), "blockers": []}
    try:
        membership, blockers = approve_exclusive_transfer(db, row, actor)
        row.review_notes = payload.review_notes
        if blockers:
            db.commit()
            return {"request": transfer_payload(row, blockers), "membership": None, "blockers": blockers}
        db.commit()
        return {"request": transfer_payload(row), "membership": membership_payload(membership), "blockers": []}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/network/transfer-requests/{request_id}/reject")
def reject_transfer(request_id: int, payload: ReviewInput, db: Session = Depends(get_db), actor: User = Depends(require_root_user)):
    require_network_schema(db)
    row = db.query(TechnicianTransferRequest).filter(TechnicianTransferRequest.id == request_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    if row.status not in {"TRANSFER_REQUESTED", "TRANSFER_PENDING_BLOCKED"}:
        raise HTTPException(status_code=409, detail="Solicitação não está pendente")
    row.status = "REJECTED"
    row.reviewed_at = datetime.utcnow()
    row.reviewed_by_user_id = actor.id
    row.review_notes = payload.review_notes
    db.commit()
    return transfer_payload(row)


@router.get("/admin/users/{user_id}/memberships")
def admin_user_memberships(user_id: int, db: Session = Depends(get_db), actor: User = Depends(require_root_user)):
    require_network_schema(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    requests = db.query(TechnicianTransferRequest).filter(TechnicianTransferRequest.user_id == user_id).order_by(TechnicianTransferRequest.created_at.desc()).all()
    return {"items": [membership_payload(row) for row in db.query(OrganizationMembership).filter(OrganizationMembership.user_id == user_id).order_by(OrganizationMembership.is_primary.desc(), OrganizationMembership.id.asc()).all()], "transfer_requests": [transfer_payload(row) for row in requests]}
