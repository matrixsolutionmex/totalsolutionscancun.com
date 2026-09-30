from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.models.technician_compensation import TechnicianCompensationPolicy
from app.services.technician_compensation_service import (
    CompensationError,
    activate_policy,
    compensation_policy_enabled_for_organization,
    create_policy,
    freeze_snapshot,
    get_snapshot,
    list_technician_snapshots,
    policy_payload,
    preview_order,
    propose_snapshot,
    quote_items_for_order,
    snapshot_payload,
    technician_snapshot_payload,
)


router = APIRouter(prefix="/technician-compensation", tags=["technician-compensation"])


class PolicyInput(BaseModel):
    currency: str = Field(default="MXN", min_length=3, max_length=8)
    effective_from: datetime
    idempotency_key: str = Field(min_length=1, max_length=160)
    technician_share_bps: int = Field(default=7500, ge=0, le=10000)
    organization_share_bps: int = Field(default=2500, ge=0, le=10000)
    default_guarantee_days: int = Field(default=7, ge=0, le=3650)


class SnapshotInput(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    category_by_item_id: dict[int, str] = Field(default_factory=dict)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except CompensationError as exc:
        status = 503 if exc.code in {
            "COMPENSATION_POLICY_UNAVAILABLE", "COMPENSATION_POLICY_CONFIGURATION_INVALID",
            "MEMBERSHIP_GATE_UNAVAILABLE",
        } else 409 if exc.code in {
            "ACTIVE_POLICY_MISSING", "ITEM_CLASSIFICATION_REQUIRED", "LABOR_BASE_EMPTY",
            "DISCOUNT_EXCEEDS_ORGANIZATION_SHARE", "SNAPSHOT_NOT_PROPOSED", "SNAPSHOT_NOT_DRAFT",
            "APPROVER_CANNOT_BE_TECHNICIAN", "POLICY_NOT_DRAFT", "ORDER_BEFORE_POLICY_EFFECTIVE_DATE",
            "PRE_POLICY_LEGACY", "POLICY_EFFECTIVE_FROM_PAST", "POLICY_EFFECTIVE_FROM_NOT_AFTER_PREVIOUS",
        } else 403 if exc.code in {"ADMIN_REQUIRED", "TENANT_MISMATCH", "SNAPSHOT_NOT_FOUND"} else 422
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.get("/policies")
def list_policies(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    if not compensation_policy_enabled_for_organization(db, actor.organization_id):
        raise HTTPException(status_code=503, detail="compensation_policy_unavailable")
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="admin_required")
    rows = db.query(TechnicianCompensationPolicy).filter_by(
        organization_id=actor.organization_id,
    ).order_by(TechnicianCompensationPolicy.version.desc()).all()
    return {"items": [policy_payload(row) for row in rows]}


@router.get("/policies/active")
def active_policy(currency: str = "MXN", db: Session = Depends(get_db), actor=Depends(get_current_user)):
    from app.services.technician_compensation_service import _active_policy, _currency
    if not compensation_policy_enabled_for_organization(db, actor.organization_id):
        raise HTTPException(status_code=503, detail="compensation_policy_unavailable")
    return policy_payload(_call(_active_policy, db, actor.organization_id, _currency(currency)))


@router.get("/availability")
def compensation_availability(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    return {"enabled": compensation_policy_enabled_for_organization(db, actor.organization_id)}


@router.post("/policies", status_code=201)
def create_policy_route(payload: PolicyInput, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    row = _call(create_policy, db, actor=actor, organization_id=actor.organization_id, **payload.model_dump())
    db.commit()
    db.refresh(row)
    return policy_payload(row)


@router.post("/policies/{policy_id}/activate")
def activate_policy_route(policy_id: int, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    row = _call(activate_policy, db, actor=actor, policy_id=policy_id)
    db.commit()
    db.refresh(row)
    return policy_payload(row)


@router.post("/orders/{order_id}/preview")
def preview_order_route(order_id: int, payload: SnapshotInput, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    return _call(preview_order, db, actor=actor, order_id=order_id, category_by_item_id=payload.category_by_item_id)


@router.get("/orders/{order_id}/quote-items")
def quote_items_route(order_id: int, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    rows = _call(quote_items_for_order, db, actor=actor, order_id=order_id)
    return {"items": rows}


@router.post("/orders/{order_id}/snapshots", status_code=201)
def propose_snapshot_route(order_id: int, payload: SnapshotInput, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    row = _call(propose_snapshot, db, actor=actor, order_id=order_id, **payload.model_dump())
    db.commit()
    db.refresh(row)
    return snapshot_payload(row)


@router.post("/snapshots/{snapshot_id}/freeze")
def freeze_snapshot_route(snapshot_id: int, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    row = _call(freeze_snapshot, db, actor=actor, snapshot_id=snapshot_id)
    db.commit()
    db.refresh(row)
    return snapshot_payload(row)


@router.get("/snapshots/{snapshot_id}")
def get_snapshot_route(snapshot_id: int, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    row = _call(get_snapshot, db, actor=actor, snapshot_id=snapshot_id)
    return snapshot_payload(row)


@router.get("/me/snapshots")
def list_my_snapshots(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    rows = _call(list_technician_snapshots, db, actor=actor)
    return {"items": [technician_snapshot_payload(row) for row in rows]}
