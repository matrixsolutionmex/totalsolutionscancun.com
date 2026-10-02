from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.models.technician_earning import TechnicianEarning
from app.models.user import User
from app.services.technician_earning_service import earning_payload, technician_earnings_enabled_for_user


router = APIRouter(prefix="/technician/earnings", tags=["technician-earnings"])


def _require_enabled(db: Session, actor: User):
    if not technician_earnings_enabled_for_user(db, actor):
        raise HTTPException(status_code=503, detail="technician_earnings_unavailable")


def _query(db: Session, actor: User):
    if actor.role not in {"BROKER", "TECNICO", "TÉCNICO"}:
        raise HTTPException(status_code=403, detail="technician_earnings_not_permitted")
    return db.query(TechnicianEarning).filter(
        TechnicianEarning.organization_id == actor.organization_id,
        TechnicianEarning.technician_user_id == actor.id,
    )


@router.get("/me")
def list_my_earnings(
    status: str | None = Query(default=None, max_length=32),
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _require_enabled(db, actor)
    query = _query(db, actor)
    if status:
        query = query.filter(TechnicianEarning.status == status.strip().upper())
    if month:
        try:
            start = datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_month") from exc
        end = datetime(datetime.strptime(month, "%Y-%m").year + (1 if start.month == 12 else 0), 1 if start.month == 12 else start.month + 1, 1)
        query = query.filter(TechnicianEarning.created_at >= start, TechnicianEarning.created_at < end)
    rows = query.order_by(TechnicianEarning.created_at.desc(), TechnicianEarning.id.desc()).offset(offset).limit(limit).all()
    return {"items": [earning_payload(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/me/summary")
def my_earnings_summary(db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    _require_enabled(db, actor)
    query = _query(db, actor)
    query = query.filter(
        or_(
            TechnicianEarning.status != "REVERSED",
            TechnicianEarning.source_type == "AUTOMATED_ADJUSTMENT",
        )
    )
    grouped = query.with_entities(
        TechnicianEarning.currency,
        func.coalesce(
            func.sum(
                case(
                    (TechnicianEarning.source_type == "AUTOMATED_ADJUSTMENT", -TechnicianEarning.net_amount),
                    else_=TechnicianEarning.net_amount,
                )
            ),
            0,
        ),
        func.count(TechnicianEarning.id),
    ).group_by(TechnicianEarning.currency).order_by(TechnicianEarning.currency).all()
    by_currency = [
        {"currency": currency, "net_total": str(total), "items": int(items)}
        for currency, total, items in grouped
    ]
    return {
        "currency": by_currency[0]["currency"] if len(by_currency) == 1 else None,
        "net_total": by_currency[0]["net_total"] if len(by_currency) == 1 else None,
        "items": sum(item["items"] for item in by_currency),
        "by_currency": by_currency,
    }


@router.get("/me/{earning_id}")
def get_my_earning(earning_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    _require_enabled(db, actor)
    row = _query(db, actor).filter(TechnicianEarning.id == earning_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="earning_not_found")
    return earning_payload(row)
