import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db, require_admin_user
from app.services.billing_provider import MockBillingProvider
from app.models.payment import Payment
from app.services.entitlement_service import account_snapshot, normalize_plan, plan_catalog
from app.services.payment_service import stripe_is_configured
from app.services.commercial_upgrade_service import (
    activate_upgrade_intent,
    cancel_upgrade_intent,
    checkout_url_for,
    create_or_reuse_upgrade_intent,
    get_active_upgrade_intent,
    list_upgrade_intents,
    mark_payment_confirmed,
    serialize_upgrade_intent,
)


router = APIRouter(prefix="/commercial", tags=["commercial"])


class MockPlanChangeRequest(BaseModel):
    plan: str = Field(min_length=1, max_length=20)
    reason: str | None = Field(default=None, max_length=240)


class UpgradeIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str = Field(min_length=1, max_length=20)


@router.get("/plans")
def commercial_plans():
    return {"plans": plan_catalog(), "billing_enabled": stripe_is_configured(), "provider": "STRIPE" if stripe_is_configured() else "CLIP"}


@router.get("/account")
def commercial_account(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    snapshot = account_snapshot(db, actor)
    active_intent = get_active_upgrade_intent(db, actor)
    snapshot["active_intent"] = serialize_upgrade_intent(active_intent)
    if active_intent and snapshot["active_intent"]:
        payment = db.query(Payment).filter(Payment.upgrade_intent_id == active_intent.id).first()
        if payment and payment.checkout_url:
            snapshot["active_intent"]["checkout_url"] = payment.checkout_url
    return snapshot


@router.get("/usage")
def commercial_usage(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    snapshot = account_snapshot(db, actor)
    return {"plan": snapshot["plan"], "limits": snapshot["limits"], "usage": snapshot["usage"]}


@router.post("/upgrade-intents")
def commercial_upgrade_intent(
    payload: UpgradeIntentRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    intent, reused = create_or_reuse_upgrade_intent(db, actor, payload.plan, request=request)
    payment = db.query(Payment).filter(Payment.upgrade_intent_id == intent.id).first()
    return {
        "intent_id": intent.id,
        "plan": intent.requested_plan,
        "provider": intent.provider,
        "checkout_url": (payment.checkout_url if payment and payment.checkout_url else checkout_url_for(intent.requested_plan)) if intent.status in {"CHECKOUT_OPENED", "PAYMENT_PENDING"} else None,
        "payment_id": payment.id if payment else None,
        "payment_status": payment.status if payment else None,
        "status": "PAYMENT_CONFIRMED" if intent.status == "PAID" else intent.status,
        "reused": reused,
    }


@router.get("/upgrade-intents")
def commercial_upgrade_intents(
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    return {"intents": list_upgrade_intents(db, actor)}


@router.post("/upgrade-intents/{intent_id}/confirm-payment")
def commercial_confirm_upgrade_payment(
    intent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    intent = mark_payment_confirmed(db, actor, intent_id, request=request)
    return {"intent_id": intent.id, "status": intent.status}


@router.post("/upgrade-intents/{intent_id}/activate")
def commercial_activate_upgrade(
    intent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    intent, subscription = activate_upgrade_intent(db, actor, intent_id, request=request)
    return {"intent_id": intent.id, "status": intent.status, "plan": subscription.plan}


@router.post("/upgrade-intents/{intent_id}/cancel")
def commercial_cancel_upgrade(
    intent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor=Depends(require_admin_user),
):
    intent = cancel_upgrade_intent(db, actor, intent_id, request=request)
    return {"intent_id": intent.id, "status": intent.status}


@router.post("/mock/plan")
def commercial_mock_plan(payload: MockPlanChangeRequest, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    if actor.role != "ROOT" and os.getenv("ALLOW_MOCK_BILLING") != "1":
        raise HTTPException(status_code=403, detail="Alteração simulada de plano exige autorização administrativa")
    plan = normalize_plan(payload.plan)
    if plan not in {"FREE", "PRO", "BUSINESS"}:
        raise HTTPException(status_code=400, detail="Plano inválido")
    subscription = MockBillingProvider().change_plan(db, actor, plan, payload.reason)
    return {"plan": subscription.plan, "status": subscription.status, "provider": subscription.provider,
            "message": "Acesso promocional liberado. Pagamento online em breve."}
