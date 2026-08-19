import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.services.billing_provider import MockBillingProvider
from app.services.entitlement_service import account_snapshot, normalize_plan, plan_catalog


router = APIRouter(prefix="/commercial", tags=["commercial"])


class MockPlanChangeRequest(BaseModel):
    plan: str = Field(min_length=1, max_length=20)
    reason: str | None = Field(default=None, max_length=240)


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
