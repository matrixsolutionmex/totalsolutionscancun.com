from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db, require_admin_user
from app.models.segmentation_referral import CampaignContact, CampaignSuppression, TechnicianReferral
from app.models.user import User
from app.services.segmentation_referral_service import (
    add_suppression,
    campaign_eligibility,
    create_referral,
    recalculate_fit_score,
    release_reward,
    reserve_reward,
    reward_eligibility,
    transition_referral,
    use_reward,
)

router = APIRouter(prefix="/admin/segmentation-referrals", tags=["segmentation-referrals"])


class SuppressionIn(BaseModel):
    email: str
    reason: str


class ContactIn(BaseModel):
    email: str
    name: str | None = None
    company: str | None = None
    segment: str = "OTRO"
    source: str = "MANUAL"
    country: str | None = None
    state: str | None = None
    city: str | None = None
    language: str = "es"


class ContactImportIn(BaseModel):
    contacts: list[ContactIn] = Field(min_length=1, max_length=500)


class ReferralIn(BaseModel):
    organization_id: int | None = None
    referrer_type: str = Field(min_length=1, max_length=24)
    referrer_id: int
    referred_name: str | None = None
    referred_email: str | None = None
    referred_phone: str | None = None


class ReferralReviewIn(BaseModel):
    status: str
    rejection_reason: str | None = None


class RewardOrderIn(BaseModel):
    service_order_id: int


def _organization_id(actor: User, requested: int | None = None) -> int:
    if actor.role != "ROOT" and requested is not None and requested != actor.organization_id:
        raise HTTPException(status_code=403, detail="Organizacao fora do escopo")
    if requested is not None:
        return requested
    if actor.organization_id is None:
        raise HTTPException(status_code=400, detail="Organizacao obrigatoria")
    return actor.organization_id


def _scoped_referral(db: Session, actor: User, referral_id: int) -> TechnicianReferral:
    row = db.query(TechnicianReferral).filter(TechnicianReferral.id == referral_id).first()
    if not row or (actor.role != "ROOT" and row.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Indicacao nao encontrada")
    return row


@router.post("/contacts/{contact_id}/fit-score")
def fit_score(contact_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    contact = db.query(CampaignContact).filter(CampaignContact.id == contact_id).first()
    if not contact or (actor.role != "ROOT" and contact.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Contato nao encontrado")
    recalculate_fit_score(db, contact, actor.id)
    db.commit()
    return {"id": contact.id, "fit_score": contact.fit_score, "breakdown": contact.breakdown(),
            "campaign_eligibility": campaign_eligibility(db, contact)}


@router.post("/suppressions")
def suppress(payload: SuppressionIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = add_suppression(db, _organization_id(actor), payload.email, payload.reason, actor.id)
    db.commit()
    return {"id": row.id, "email": row.normalized_email, "reason": row.reason}


@router.post("/contacts/import")
def import_contacts(payload: ContactImportIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    organization_id = _organization_id(actor)
    created = updated = suppressed = 0
    for item in payload.contacts:
        normalized = item.email.strip().lower()
        contact = db.query(CampaignContact).filter(
            CampaignContact.organization_id == organization_id,
            CampaignContact.normalized_email == normalized,
        ).first()
        if contact is None:
            contact = CampaignContact(organization_id=organization_id, normalized_email=normalized)
            db.add(contact)
            db.flush()
            created += 1
        else:
            updated += 1
        contact.email = item.email.strip()
        contact.name = item.name
        contact.company = item.company
        contact.segment = item.segment.upper()
        contact.source = item.source.upper()
        contact.country = item.country
        contact.state = item.state
        contact.city = item.city
        contact.language = item.language.lower()
        recalculate_fit_score(db, contact, actor.id)
        if campaign_eligibility(db, contact)["reason"] == "SUPPRESSED":
            suppressed += 1
    db.commit()
    return {"organization_id": organization_id, "created": created, "updated": updated,
            "suppressed": suppressed, "total": len(payload.contacts)}


@router.post("/referrals")
def referral(payload: ReferralIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row, duplicate = create_referral(
        db, _organization_id(actor, payload.organization_id), payload.referrer_type, payload.referrer_id,
        payload.referred_name, payload.referred_email, payload.referred_phone, actor.id,
    )
    db.commit()
    return {"id": row.id, "referral_code": row.referral_code, "status": row.status,
            "possible_duplicate": duplicate}


@router.get("/referrals/{referral_id}")
def get_referral(referral_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = _scoped_referral(db, actor, referral_id)
    return {"id": row.id, "organization_id": row.organization_id, "referral_code": row.referral_code,
            "status": row.status, "referred_name": row.referred_name, "referred_email": row.referred_email}


@router.post("/referrals/{referral_id}/review")
def review_referral(referral_id: int, payload: ReferralReviewIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = _scoped_referral(db, actor, referral_id)
    transition_referral(db, row, payload.status, actor.id, payload.rejection_reason)
    db.commit()
    return {"id": row.id, "status": row.status}


@router.get("/service-orders/{service_order_id}/reward-eligibility")
def reward_check(service_order_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    return reward_eligibility(db, actor, service_order_id)


@router.get("/referrals/{referral_id}/campaign-eligibility")
def referral_campaign_eligibility(referral_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = _scoped_referral(db, actor, referral_id)
    return {"referral_id": row.id, "eligible": row.status == "APPROVED"}


@router.post("/rewards/{reward_id}/reserve")
def reserve(reward_id: int, payload: RewardOrderIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = reserve_reward(db, actor, reward_id, payload.service_order_id)
    db.commit()
    return {"id": row.id, "status": row.status, "service_order_id": row.reserved_service_order_id}


@router.post("/rewards/{reward_id}/release")
def release(reward_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = release_reward(db, actor, reward_id)
    db.commit()
    return {"id": row.id, "status": row.status}


@router.post("/rewards/{reward_id}/use")
def use(reward_id: int, payload: RewardOrderIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = use_reward(db, actor, reward_id, payload.service_order_id)
    db.commit()
    return {"id": row.id, "status": row.status, "service_order_id": row.used_service_order_id}
