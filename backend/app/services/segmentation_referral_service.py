import json
import re
import secrets
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.segmentation_referral import (
    CampaignContact,
    CampaignSuppression,
    ReferralReward,
    SegmentationReferralAuditEvent,
    TechnicianReferral,
)
from app.models.service_order import ServiceOrder
from app.models.lead import Lead
from app.models.user import User


REFERRAL_STATUSES = {
    "PENDING", "REGISTERED", "UNDER_REVIEW", "APPROVED", "REJECTED",
    "CANCELLED", "EXPIRED",
}
REWARD_STATUSES = {"REWARD_AVAILABLE", "REWARD_RESERVED", "REWARD_USED", "REWARD_EXPIRED"}
ACTIVE_DUPLICATE_STATUSES = {"PENDING", "REGISTERED", "UNDER_REVIEW", "APPROVED", "REWARD_AVAILABLE", "REWARD_RESERVED"}
KNOWN_SEGMENTS = {"HOTEL", "AIRBNB", "INMOBILIARIA", "CONDOMINIO", "ADMINISTRADOR", "OFICINA", "COMERCIO", "EMPRESA"}
SUPPRESSION_REASONS = {"UNSUBSCRIBE", "BOUNCE", "COMPLAINT", "MANUAL_BLOCK", "INVALID_EMAIL"}
TRANSITIONS = {
    "PENDING": {"REGISTERED", "UNDER_REVIEW", "CANCELLED", "EXPIRED"},
    "REGISTERED": {"UNDER_REVIEW", "CANCELLED", "EXPIRED"},
    "UNDER_REVIEW": {"APPROVED", "REJECTED", "CANCELLED", "EXPIRED"},
    "APPROVED": {"CANCELLED"},
}


def normalize_email(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized or None


def normalize_phone(value: str | None) -> str | None:
    normalized = re.sub(r"\D", "", value or "")
    return normalized or None


def _audit(db: Session, organization_id: int, event_type: str, actor_id: int | None = None, **ids):
    event = SegmentationReferralAuditEvent(
        organization_id=organization_id,
        actor_id=actor_id,
        event_type=event_type,
        contact_id=ids.get("contact_id"),
        referral_id=ids.get("referral_id"),
        reward_id=ids.get("reward_id"),
        service_order_id=ids.get("service_order_id"),
        previous_status=ids.get("previous_status"),
        new_status=ids.get("new_status"),
        metadata_json=json.dumps(ids.get("metadata", {}), sort_keys=True),
    )
    db.add(event)
    return event


def calculate_fit_score(contact: CampaignContact) -> tuple[int, dict]:
    segment_known = (contact.segment or "").upper() in KNOWN_SEGMENTS
    has_location = bool(contact.city and contact.country)
    has_partial_location = bool(contact.state or contact.country)
    has_business_identity = bool(contact.company or contact.name)
    valid_email = bool(normalize_email(contact.email) and "@" in contact.email)
    breakdown = {
        "segment": 30 if segment_known else 0,
        "location": 20 if has_location else 15 if has_partial_location else 0,
        "business_identity": 20 if contact.company else 10 if contact.name else 0,
        "service_relevance": 20 if segment_known else 0,
        "data_quality": 10 if valid_email and contact.language else 8 if valid_email else 0,
    }
    return min(100, sum(breakdown.values())), breakdown


def recalculate_fit_score(db: Session, contact: CampaignContact, actor_id: int | None = None) -> CampaignContact:
    score, breakdown = calculate_fit_score(contact)
    contact.fit_score = score
    contact.fit_score_breakdown = json.dumps(breakdown, sort_keys=True)
    contact.fit_score_version = "v1"
    contact.fit_score_calculated_at = datetime.utcnow()
    _audit(db, contact.organization_id, "CONTACT_FIT_SCORE_RECALCULATED", actor_id, contact_id=contact.id,
           metadata={"fit_score": score, "version": "v1"})
    return contact


def is_suppressed(db: Session, organization_id: int, email: str | None) -> bool:
    normalized = normalize_email(email)
    if not normalized:
        return False
    return db.query(CampaignSuppression.id).filter(
        CampaignSuppression.organization_id == organization_id,
        CampaignSuppression.normalized_email == normalized,
    ).first() is not None


def campaign_eligibility(db: Session, contact: CampaignContact) -> dict:
    if contact.status != "ACTIVE":
        return {"eligible": False, "reason": "CONTACT_INACTIVE"}
    if is_suppressed(db, contact.organization_id, contact.email):
        return {"eligible": False, "reason": "SUPPRESSED"}
    if contact.fit_score <= 0:
        return {"eligible": False, "reason": "FIT_SCORE_UNAVAILABLE"}
    return {"eligible": True, "reason": None}


def add_suppression(db: Session, organization_id: int, email: str, reason: str, actor_id: int | None = None):
    reason = reason.upper()
    if reason not in SUPPRESSION_REASONS:
        raise HTTPException(status_code=422, detail="Motivo de suppression invalido")
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        raise HTTPException(status_code=422, detail="Email invalido")
    row = db.query(CampaignSuppression).filter_by(
        organization_id=organization_id, normalized_email=normalized
    ).first()
    if row:
        return row
    row = CampaignSuppression(
        organization_id=organization_id,
        normalized_email=normalized,
        reason=reason,
        created_by_user_id=actor_id,
    )
    db.add(row)
    _audit(db, organization_id, "SUPPRESSION_ADDED", actor_id, metadata={"reason": reason})
    return row


def _new_referral_code(db: Session) -> str:
    for _ in range(10):
        code = "TS-" + secrets.token_urlsafe(8).replace("-", "").replace("_", "").upper()[:10]
        if not db.query(TechnicianReferral.id).filter_by(referral_code=code).first():
            return code
    raise HTTPException(status_code=500, detail="Nao foi possivel gerar codigo de indicacao")


def find_referral_duplicate(db: Session, organization_id: int, email: str | None, phone: str | None):
    normalized_email = normalize_email(email)
    normalized_phone = normalize_phone(phone)
    query = db.query(TechnicianReferral).filter(
        TechnicianReferral.organization_id == organization_id,
        TechnicianReferral.status.in_(ACTIVE_DUPLICATE_STATUSES),
    )
    for row in query.all():
        if normalized_email and row.normalized_email == normalized_email:
            return row
        if normalized_phone and row.normalized_phone == normalized_phone:
            return row
    return None


def create_referral(db: Session, organization_id: int, referrer_type: str, referrer_id: int,
                    referred_name: str | None, referred_email: str | None, referred_phone: str | None,
                    actor_id: int | None = None):
    referrer_type = referrer_type.upper()
    referrer_model = User if referrer_type == "USER" else Lead if referrer_type == "LEAD" else None
    referrer = db.query(referrer_model).filter(referrer_model.id == referrer_id).first() if referrer_model else None
    if not referrer or referrer.organization_id != organization_id:
        raise HTTPException(status_code=403, detail="Referenciador fora do escopo da organizacao")
    duplicate = find_referral_duplicate(db, organization_id, referred_email, referred_phone)
    referral = TechnicianReferral(
        organization_id=organization_id,
        referrer_type=referrer_type,
        referrer_id=referrer_id,
        referral_code=_new_referral_code(db),
        referred_name=referred_name,
        referred_email=referred_email,
        referred_phone=referred_phone,
        normalized_email=normalize_email(referred_email),
        normalized_phone=normalize_phone(referred_phone),
        status="PENDING",
    )
    db.add(referral)
    db.flush()
    _audit(db, organization_id, "REFERRAL_CREATED", actor_id, referral_id=referral.id,
           metadata={"duplicate": bool(duplicate)})
    return referral, bool(duplicate)


def transition_referral(db: Session, referral: TechnicianReferral, new_status: str, actor_id: int | None = None,
                        rejection_reason: str | None = None) -> TechnicianReferral:
    new_status = new_status.upper()
    if new_status not in REFERRAL_STATUSES or new_status not in TRANSITIONS.get(referral.status, set()):
        raise HTTPException(status_code=409, detail="Transicao de referral invalida")
    previous = referral.status
    referral.status = new_status
    if new_status == "REGISTERED":
        referral.registered_at = datetime.utcnow()
    if new_status == "APPROVED":
        referral.approved_at = datetime.utcnow()
        referral.approved_by = actor_id
        _ensure_reward(db, referral, actor_id)
    if new_status == "REJECTED":
        referral.rejected_at = datetime.utcnow()
        referral.rejected_by = actor_id
        referral.rejection_reason = rejection_reason
    _audit(db, referral.organization_id, "REFERRAL_STATUS_CHANGED", actor_id, referral_id=referral.id,
           previous_status=previous, new_status=new_status)
    return referral


def _ensure_reward(db: Session, referral: TechnicianReferral, actor_id: int | None = None) -> ReferralReward:
    reward = db.query(ReferralReward).filter_by(referral_id=referral.id).first()
    if reward:
        return reward
    reward = ReferralReward(
        organization_id=referral.organization_id,
        referral_id=referral.id,
        reward_type="PERCENT_DISCOUNT",
        reward_value=Decimal("50.00"),
        reward_cap_amount=Decimal("1000.00"),
        currency="MXN",
        status="REWARD_AVAILABLE",
    )
    db.add(reward)
    db.flush()
    _audit(db, referral.organization_id, "REWARD_CREATED", actor_id, referral_id=referral.id,
           reward_id=reward.id, new_status=reward.status,
           metadata={"reward_value": "50.00", "cap": "1000.00", "currency": "MXN"})
    _audit(db, referral.organization_id, "REWARD_AVAILABLE", actor_id, referral_id=referral.id,
           reward_id=reward.id, new_status=reward.status)
    return reward


def reward_eligibility(db: Session, actor, service_order_id: int) -> dict:
    order = db.query(ServiceOrder).filter(ServiceOrder.id == service_order_id).first()
    if not order or (actor.role != "ROOT" and order.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Ordem de servico nao encontrada")
    reward = db.query(ReferralReward).join(TechnicianReferral).filter(
        ReferralReward.organization_id == order.organization_id,
        ReferralReward.status == "REWARD_AVAILABLE",
        TechnicianReferral.referrer_type == "LEAD",
        TechnicianReferral.referrer_id == order.lead_id,
    ).first()
    if not reward:
        return {"eligible": False, "reason": "NO_ELIGIBLE_REFERRAL", "service_order_id": service_order_id}
    if not order.final_service_price or Decimal(str(order.final_service_price)) <= 0:
        return {"eligible": False, "reason": "ORDER_NOT_ELIGIBLE", "service_order_id": service_order_id}
    if (order.status or "").upper() in {"CANCELADA", "CANCELLED", "CANCELED", "ANULADA", "REJECTED"}:
        return {"eligible": False, "reason": "ORDER_CANCELLED", "service_order_id": service_order_id}
    prior_eligible = db.query(ServiceOrder.id).filter(
        ServiceOrder.organization_id == order.organization_id,
        ServiceOrder.lead_id == order.lead_id,
        ServiceOrder.id < order.id,
        ServiceOrder.final_service_price > 0,
        ServiceOrder.status.notin_({"CANCELADA", "CANCELLED", "CANCELED", "ANULADA", "REJECTED"}),
    ).first()
    if prior_eligible:
        return {"eligible": False, "reason": "FIRST_ELIGIBLE_ORDER_ALREADY_EXISTS", "service_order_id": service_order_id}
    price = Decimal(str(order.final_service_price))
    potential_discount = min((price * reward.reward_value / Decimal("100")), reward.reward_cap_amount)
    return {
        "eligible": True,
        "reason": None,
        "service_order_id": service_order_id,
        "reward_id": reward.id,
        "reward_value_percent": str(reward.reward_value),
        "reward_cap_amount": str(reward.reward_cap_amount),
        "currency": reward.currency,
        "first_eligible_order_only": True,
        "potential_discount": str(potential_discount.quantize(Decimal("0.01"))),
        "applied": False,
    }


def reserve_reward(db: Session, actor, reward_id: int, service_order_id: int) -> ReferralReward:
    reward = db.query(ReferralReward).filter(ReferralReward.id == reward_id).first()
    if not reward or (actor.role != "ROOT" and reward.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Reward nao encontrado")
    if reward.status != "REWARD_AVAILABLE":
        raise HTTPException(status_code=409, detail="Reward indisponivel")
    order = db.query(ServiceOrder).filter(ServiceOrder.id == service_order_id).first()
    if not order or order.organization_id != reward.organization_id:
        raise HTTPException(status_code=404, detail="Ordem de servico nao encontrada")
    reward.status = "REWARD_RESERVED"
    reward.reserved_service_order_id = service_order_id
    reward.reserved_at = datetime.utcnow()
    _audit(db, reward.organization_id, "REWARD_RESERVED", actor.id, reward_id=reward.id,
           service_order_id=service_order_id, previous_status="REWARD_AVAILABLE", new_status=reward.status)
    return reward


def release_reward(db: Session, actor, reward_id: int) -> ReferralReward:
    reward = db.query(ReferralReward).filter(ReferralReward.id == reward_id).first()
    if not reward or (actor.role != "ROOT" and reward.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Reward nao encontrado")
    if reward.status != "REWARD_RESERVED":
        raise HTTPException(status_code=409, detail="Reward nao esta reservado")
    reward.status = "REWARD_AVAILABLE"
    reward.reserved_service_order_id = None
    reward.reserved_at = None
    _audit(db, reward.organization_id, "REWARD_RELEASED", actor.id, reward_id=reward.id,
           previous_status="REWARD_RESERVED", new_status=reward.status)
    return reward


def use_reward(db: Session, actor, reward_id: int, service_order_id: int) -> ReferralReward:
    reward = db.query(ReferralReward).filter(ReferralReward.id == reward_id).first()
    if not reward or (actor.role != "ROOT" and reward.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Reward nao encontrado")
    if reward.status != "REWARD_RESERVED" or reward.reserved_service_order_id != service_order_id:
        raise HTTPException(status_code=409, detail="Reward nao reservado para esta ordem")
    reward.status = "REWARD_USED"
    reward.used_service_order_id = service_order_id
    reward.used_at = datetime.utcnow()
    _audit(db, reward.organization_id, "REWARD_USED", actor.id, reward_id=reward.id,
           service_order_id=service_order_id, previous_status="REWARD_RESERVED", new_status=reward.status)
    return reward
