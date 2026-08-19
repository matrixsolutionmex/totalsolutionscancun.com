from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth_security import audit_auth_event
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.organization import Organization
from app.models.user import User
from app.services.billing_provider import MockBillingProvider
from app.services.entitlement_service import PLANS, current_plan, normalize_plan


CHECKOUT_URLS = {
    "PRO": "https://pago.clip.mx/v3/5210f95c-eb87-4c85-bdd6-d4d84c7255d0",
    "BUSINESS": "https://pago.clip.mx/v3/e578ef48-70c2-47e6-8aa3-c459c23b6ec4",
}
ACTIVE_INTENT_STATUSES = {"CHECKOUT_OPENED", "PAYMENT_PENDING", "PAYMENT_CONFIRMED", "PAID"}
TERMINAL_INTENT_STATUSES = {"ACTIVATED", "CANCELLED", "ABANDONED", "SUPERSEDED"}
CONFIRMED_INTENT_STATUSES = {"PAYMENT_CONFIRMED", "PAID"}


def checkout_url_for(plan: str) -> str:
    requested_plan = normalize_plan(plan)
    try:
        return CHECKOUT_URLS[requested_plan]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="Checkout indisponivel para este plano") from exc


def _require_admin(actor: User):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Somente root ou gerente pode administrar solicitacoes comerciais")


def _intent_or_404(db: Session, actor: User, intent_id: int) -> CommercialUpgradeIntent:
    intent = (
        db.query(CommercialUpgradeIntent)
        .filter(CommercialUpgradeIntent.id == intent_id, CommercialUpgradeIntent.organization_id == actor.organization_id)
        .first()
    )
    if not intent:
        raise HTTPException(status_code=404, detail="Solicitacao comercial nao encontrada")
    return intent


def get_active_upgrade_intent(db: Session, actor: User) -> CommercialUpgradeIntent | None:
    return (
        db.query(CommercialUpgradeIntent)
        .filter(CommercialUpgradeIntent.organization_id == actor.organization_id, CommercialUpgradeIntent.status.in_(ACTIVE_INTENT_STATUSES))
        .order_by(CommercialUpgradeIntent.created_at.desc(), CommercialUpgradeIntent.id.desc())
        .first()
    )


def serialize_upgrade_intent(intent: CommercialUpgradeIntent | None) -> dict | None:
    if not intent:
        return None
    return {
        "id": intent.id,
        "plan": intent.requested_plan,
        "status": "PAYMENT_CONFIRMED" if intent.status == "PAID" else intent.status,
        "provider": intent.provider,
        "reference_price_mxn": float(intent.reference_price_mxn),
        "checkout_url": checkout_url_for(intent.requested_plan) if intent.status in {"CHECKOUT_OPENED", "PAYMENT_PENDING"} else None,
        "created_at": intent.created_at.isoformat(),
    }


def _audit(db: Session, *, request, event_type: str, actor: User, intent: CommercialUpgradeIntent, detail: dict):
    audit_auth_event(db, request=request, event_type=event_type, outcome="SUCCESS", user=actor, actor=actor, detail={"intent_id": intent.id, **detail})


def _replace_active_intent(db: Session, actor: User, previous: CommercialUpgradeIntent, requested_plan: str, *, request=None):
    previous.status = "CANCELLED"
    previous.updated_at = datetime.utcnow()
    detail = {"previous_plan": previous.requested_plan, "new_plan": requested_plan}
    _audit(db, request=request, event_type="UPGRADE_INTENT_REPLACED", actor=actor, intent=previous, detail=detail)
    _audit(db, request=request, event_type="UPGRADE_PLAN_CHANGED", actor=actor, intent=previous, detail=detail)


def _new_intent(db: Session, actor: User, requested_plan: str, *, source: str, request=None):
    catalog_plan = PLANS[requested_plan]
    intent = CommercialUpgradeIntent(
        organization_id=actor.organization_id, user_id=actor.id, requested_plan=requested_plan,
        reference_price_mxn=Decimal(str(catalog_plan["monthly_reference"])), provider="CLIP",
        status="CHECKOUT_OPENED", source=(source or "PLANS_UI")[:80],
    )
    db.add(intent)
    db.flush()
    _audit(db, request=request, event_type="UPGRADE_INTENT_CREATED", actor=actor, intent=intent, detail={"plan": requested_plan, "provider": "CLIP"})
    return intent


def create_or_reuse_upgrade_intent(db: Session, actor: User, plan: str, *, source: str = "PLANS_UI", request=None):
    requested_plan = normalize_plan(plan)
    if requested_plan not in CHECKOUT_URLS:
        raise HTTPException(status_code=400, detail="Somente PRO ou BUSINESS possuem checkout")
    active_plan = current_plan(db, actor)
    if active_plan == requested_plan:
        raise HTTPException(status_code=409, detail=f"O plano {requested_plan} ja esta ativo")
    if active_plan == "BUSINESS":
        raise HTTPException(status_code=409, detail="BUSINESS ja esta ativo; downgrade nao faz parte deste fluxo")

    try:
        active = (
            db.query(CommercialUpgradeIntent)
            .filter(CommercialUpgradeIntent.organization_id == actor.organization_id, CommercialUpgradeIntent.status.in_(ACTIVE_INTENT_STATUSES))
            .with_for_update()
            .order_by(CommercialUpgradeIntent.created_at.desc(), CommercialUpgradeIntent.id.desc())
            .first()
        )
        if active:
            if active.requested_plan == requested_plan or active.status in CONFIRMED_INTENT_STATUSES:
                event = "UPGRADE_PAYMENT_ALREADY_CONFIRMED" if active.status in CONFIRMED_INTENT_STATUSES else "UPGRADE_INTENT_REUSED"
                _audit(db, request=request, event_type=event, actor=actor, intent=active, detail={"requested_plan": requested_plan})
                db.commit()
                db.refresh(active)
                return active, True
            _replace_active_intent(db, actor, active, requested_plan, request=request)
        intent = _new_intent(db, actor, requested_plan, source=source, request=request)
        db.commit()
        db.refresh(intent)
        return intent, False
    except IntegrityError:
        db.rollback()
        existing = get_active_upgrade_intent(db, actor)
        if not existing or existing.requested_plan != requested_plan:
            raise HTTPException(status_code=409, detail="Outra solicitacao comercial ativa foi criada; tente novamente")
        _audit(db, request=request, event_type="UPGRADE_INTENT_DUPLICATE_BLOCKED", actor=actor, intent=existing, detail={"plan": requested_plan})
        db.commit()
        db.refresh(existing)
        return existing, True


def create_upgrade_intent(db: Session, actor: User, plan: str, *, source: str = "PLANS_UI", request=None):
    return create_or_reuse_upgrade_intent(db, actor, plan, source=source, request=request)[0]


def mark_payment_confirmed(db: Session, actor: User, intent_id: int, *, request=None):
    _require_admin(actor)
    intent = _intent_or_404(db, actor, intent_id)
    if intent.status == "ACTIVATED" or intent.status in CONFIRMED_INTENT_STATUSES:
        return intent
    if intent.status not in {"CHECKOUT_OPENED", "PAYMENT_PENDING"}:
        raise HTTPException(status_code=409, detail="Solicitacao nao pode ser confirmada neste estado")
    intent.status = "PAYMENT_CONFIRMED"
    intent.updated_at = datetime.utcnow()
    _audit(db, request=request, event_type="UPGRADE_PAYMENT_CONFIRMED", actor=actor, intent=intent, detail={"plan": intent.requested_plan, "provider": intent.provider})
    db.commit()
    db.refresh(intent)
    return intent


def activate_upgrade_intent(db: Session, actor: User, intent_id: int, *, request=None):
    _require_admin(actor)
    intent = _intent_or_404(db, actor, intent_id)
    if intent.status == "ACTIVATED":
        return intent, None
    if intent.status not in CONFIRMED_INTENT_STATUSES:
        raise HTTPException(status_code=409, detail="Confirme o pagamento antes de ativar o plano")
    subscription = MockBillingProvider().change_plan(db, actor, intent.requested_plan, reason=f"upgrade_intent:{intent.id}")
    intent.status = "ACTIVATED"
    intent.activated_at = datetime.utcnow()
    intent.activated_by_user_id = actor.id
    intent.updated_at = datetime.utcnow()
    _audit(db, request=request, event_type="UPGRADE_PLAN_ACTIVATED", actor=actor, intent=intent, detail={"plan": intent.requested_plan})
    db.commit()
    db.refresh(intent)
    return intent, subscription


def cancel_upgrade_intent(db: Session, actor: User, intent_id: int, *, request=None):
    _require_admin(actor)
    intent = _intent_or_404(db, actor, intent_id)
    if intent.status == "ACTIVATED":
        raise HTTPException(status_code=409, detail="Plano ativado nao pode ser cancelado")
    if intent.status == "CANCELLED":
        return intent
    intent.status = "CANCELLED"
    intent.updated_at = datetime.utcnow()
    _audit(db, request=request, event_type="UPGRADE_CANCELLED", actor=actor, intent=intent, detail={"plan": intent.requested_plan})
    db.commit()
    db.refresh(intent)
    return intent


def normalize_existing_upgrade_intents(db: Session) -> int:
    changed = 0
    organizations = db.query(CommercialUpgradeIntent.organization_id).filter(CommercialUpgradeIntent.status.in_(ACTIVE_INTENT_STATUSES)).distinct().all()
    for (organization_id,) in organizations:
        intents = (
            db.query(CommercialUpgradeIntent)
            .filter(CommercialUpgradeIntent.organization_id == organization_id, CommercialUpgradeIntent.status.in_(ACTIVE_INTENT_STATUSES))
            .order_by(CommercialUpgradeIntent.created_at.asc(), CommercialUpgradeIntent.id.asc()).all()
        )
        for duplicate in intents[1:]:
            duplicate.status = "CANCELLED"
            duplicate.updated_at = datetime.utcnow()
            user = db.query(User).filter(User.id == duplicate.user_id).first()
            if user:
                audit_auth_event(db, request=None, event_type="UPGRADE_INTENT_DUPLICATE_BLOCKED", outcome="NORMALIZED", user=user, actor=user, detail={"intent_id": duplicate.id, "kept_intent_id": intents[0].id})
            changed += 1
    if changed:
        db.commit()
    return changed


def list_upgrade_intents(db: Session, actor: User):
    _require_admin(actor)
    rows = (
        db.query(CommercialUpgradeIntent, User, Organization)
        .join(User, User.id == CommercialUpgradeIntent.user_id)
        .join(Organization, Organization.id == CommercialUpgradeIntent.organization_id)
        .filter(CommercialUpgradeIntent.organization_id == actor.organization_id)
        .order_by(CommercialUpgradeIntent.created_at.desc()).limit(100).all()
    )
    return [{
        "id": intent.id, "user": user.full_name or user.username, "organization": organization.name,
        "plan": intent.requested_plan, "reference_price_mxn": float(intent.reference_price_mxn), "provider": intent.provider,
        "status": "PAYMENT_CONFIRMED" if intent.status == "PAID" else intent.status, "created_at": intent.created_at.isoformat(),
    } for intent, user, organization in rows]
