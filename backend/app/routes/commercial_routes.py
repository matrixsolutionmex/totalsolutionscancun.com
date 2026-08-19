import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db, require_admin_user
from app.services.billing_provider import MockBillingProvider
from app.services.entitlement_service import account_snapshot, normalize_plan, plan_catalog
from app.services.commercial_upgrade_service import (
    activate_upgrade_intent,
    cancel_upgrade_intent,
    checkout_url_for,
    create_upgrade_intent,
    list_upgrade_intents,
    mark_payment_confirmed,
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
    return {"plans": plan_catalog(), "billing_enabled": False, "provider": "MOCK"}


@router.get("/account")
def commercial_account(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    return account_snapshot(db, actor)


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
    intent = create_upgrade_intent(db, actor, payload.plan, request=request)
    return {
        "intent_id": intent.id,
        "plan": intent.requested_plan,
        "provider": intent.provider,
        "checkout_url": checkout_url_for(intent.requested_plan),
        "status": intent.status,
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
