from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db, require_root_user
from app.models.user import User
from app.schemas.auth_schema import UserApprovalRequest
from app.schemas.user_schema import UserResponse


router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users/pending", response_model=list[UserResponse])
def pending_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_root_user),
):
    return (
        db.query(User)
        .filter(User.status.in_(["PENDING_EMAIL", "PENDING_APPROVAL"]))
        .order_by(User.registered_at.desc(), User.id.desc())
        .all()
    )


@router.post("/users/{user_id}/approve", response_model=UserResponse)
def approve_user(
    user_id: int,
    payload: UserApprovalRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_root_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")
    if not user.email_verified:
        raise HTTPException(status_code=400, detail="Email ainda nao confirmado")

    role = payload.role.upper()
    if role not in {"GERENTE", "BROKER"}:
        raise HTTPException(status_code=400, detail="Role deve ser GERENTE ou BROKER")

    if role == "BROKER" and payload.manager_id is not None:
        manager = (
            db.query(User)
            .filter(
                User.id == payload.manager_id,
                User.role == "GERENTE",
                User.is_active.is_(True),
            )
            .first()
        )
        if not manager:
            raise HTTPException(status_code=400, detail="Gerente responsavel nao encontrado")

    user.role = role
    user.manager_id = payload.manager_id if role == "BROKER" else None
    user.plan = (payload.plan or user.plan or "STARTER").upper()
    if payload.plan_max_brokers is not None:
        user.plan_max_brokers = max(payload.plan_max_brokers, 0)
    if payload.plan_max_leads is not None:
        user.plan_max_leads = max(payload.plan_max_leads, 0)
    user.status = "ACTIVE"
    user.is_active = True
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/suspend", response_model=UserResponse)
def suspend_user(
    user_id: int,
    db: Session = Depends(get_db),
    root: User = Depends(require_root_user),
):
    if user_id == root.id:
        raise HTTPException(status_code=400, detail="Root nao pode suspender a propria conta")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")

    user.status = "SUSPENDED"
    user.is_active = False
    user.last_seen_at = None
    db.commit()
    db.refresh(user)
    return user
