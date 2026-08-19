from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.auth_security import audit_auth_event
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.organization import Organization
from app.models.user import User
from app.services.billing_provider import MockBillingProvider
from app.services.entitlement_service import PLANS, normalize_plan


CHECKOUT_URLS = {
    "PRO": "https://pago.clip.mx/v3/5210f95c-eb87-4c85-bdd6-d4d84c7255d0",
    "BUSINESS": "https://pago.clip.mx/v3/e578ef48-70c2-47e6-8aa3-c459c23b6ec4",
}
PAID_INTENT_STATUSES = {"PAID", "ACTIVATED"}
TERMINAL_INTENT_STATUSES = {"ACTIVATED", "CANCELLED", "ABANDONED"}


def checkout_url_for(plan: str) -> str:
    try:
        return CHECKOUT_URLS[normalize_plan(plan)]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="Checkout indisponivel para este plano") from exc


def _intent_or_404(db: Session, actor: User, intent_id: int) -> CommercialUpgradeIntent:
    intent = (
        db.query(CommercialUpgradeIntent)
        .filter(
            CommercialUpgradeIntent.id == intent_id,
            CommercialUpgradeIntent.organization_id == actor.organization_id,
        )
        .first()
    )
    if not intent:
        raise HTTPException(status_code=404, detail="Solicitacao comercial nao encontrada")
    return intent


def _require_admin(actor: User):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Somente root ou gerente pode administrar solicitacoes comerciais")


def create_upgrade_intent(db: Session, actor: User, plan: str, *, source: str = "PLANS_UI", request=None):
    requested_plan = normalize_plan(plan)
    if requested_plan not in CHECKOUT_URLS:
        raise HTTPException(status_code=400, detail="Somente PRO ou BUSINESS possuem checkout")
    catalog_plan = PLANS[requested_plan]
    intent = CommercialUpgradeIntent(
        organization_id=actor.organization_id,
        user_id=actor.id,
        requested_plan=requested_plan,
        reference_price_mxn=Decimal(str(catalog_plan["monthly_reference"])),
        provider="CLIP",
        status="CHECKOUT_OPENED",
        source=(source or "PLANS_UI")[:80],
    )
    db.add(intent)
    db.flush()
    audit_auth_event(
        db,
        request=request,
        event_type="UPGRADE_INTENT_CREATED",
        outcome="SUCCESS",
        user=actor,
        actor=actor,
        detail={"intent_id": intent.id, "plan": requested_plan, "provider": "CLIP"},
    )
    db.commit()
    db.refresh(intent)
    return intent


def mark_payment_confirmed(db: Session, actor: User, intent_id: int, *, request=None):
    _require_admin(actor)
    intent = _intent_or_404(db, actor, intent_id)
    if intent.status == "ACTIVATED":
        return intent
    if intent.status == "PAID":
        return intent
    if intent.status not in {"CHECKOUT_OPENED", "PAYMENT_PENDING"}:
        raise HTTPException(status_code=409, detail="Solicitacao nao pode ser confirmada neste estado")
    intent.status = "PAID"
    intent.updated_at = datetime.utcnow()
    audit_auth_event(
        db,
        request=request,
        event_type="UPGRADE_PAYMENT_CONFIRMED",
        outcome="SUCCESS",
        user=actor,
        actor=actor,
        detail={"intent_id": intent.id, "plan": intent.requested_plan, "provider": intent.provider},
    )
    db.commit()
    db.refresh(intent)
    return intent


def activate_upgrade_intent(db: Session, actor: User, intent_id: int, *, request=None):
    _require_admin(actor)
    intent = _intent_or_404(db, actor, intent_id)
    if intent.status == "ACTIVATED":
        return intent
    if intent.status != "PAID":
        raise HTTPException(status_code=409, detail="Confirme o pagamento antes de ativar o plano")

    subscription = MockBillingProvider().change_plan(
        db, actor, intent.requested_plan, reason=f"upgrade_intent:{intent.id}"
    )
    intent.status = "ACTIVATED"
    intent.activated_at = datetime.utcnow()
    intent.activated_by_user_id = actor.id
    intent.updated_at = datetime.utcnow()
    audit_auth_event(
        db,
        request=request,
        event_type="UPGRADE_PLAN_ACTIVATED",
        outcome="SUCCESS",
        user=actor,
        actor=actor,
        detail={"intent_id": intent.id, "plan": intent.requested_plan},
    )
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
    audit_auth_event(
        db,
        request=request,
        event_type="UPGRADE_CANCELLED",
        outcome="SUCCESS",
        user=actor,
        actor=actor,
        detail={"intent_id": intent.id, "plan": intent.requested_plan},
    )
    db.commit()
    db.refresh(intent)
    return intent


def list_upgrade_intents(db: Session, actor: User):
    _require_admin(actor)
    rows = (
        db.query(CommercialUpgradeIntent, User, Organization)
        .join(User, User.id == CommercialUpgradeIntent.user_id)
        .join(Organization, Organization.id == CommercialUpgradeIntent.organization_id)
        .filter(CommercialUpgradeIntent.organization_id == actor.organization_id)
        .order_by(CommercialUpgradeIntent.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": intent.id,
            "user": user.full_name or user.username,
            "organization": organization.name,
            "plan": intent.requested_plan,
            "reference_price_mxn": float(intent.reference_price_mxn),
            "provider": intent.provider,
            "status": intent.status,
            "created_at": intent.created_at.isoformat(),
        }
        for intent, user, organization in rows
    ]
