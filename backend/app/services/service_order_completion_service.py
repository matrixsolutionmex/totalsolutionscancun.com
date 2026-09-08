from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.auth_security import audit_auth_event
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import (
    ServiceOrderCustomerAcceptance,
    ServiceOrderTechnicalCompletion,
    ServiceOrderWarranty,
)
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.service_order_quote import ServiceOrderQuote
from app.services.service_order_payment_plan_service import payment_plan_projection
from app.services.service_order_quote_service import scoped_order


def _now() -> datetime:
    return datetime.utcnow()


def _completion_schema_available(db: Session) -> bool:
    """Keep public projections readable during rolling schema upgrades."""
    bind = db.get_bind()
    inspector = inspect(bind)
    return all(
        inspector.has_table(table)
        for table in (
            ServiceOrderTechnicalCompletion.__tablename__,
            ServiceOrderCustomerAcceptance.__tablename__,
            ServiceOrderWarranty.__tablename__,
        )
    )


def _completion(db: Session, order: ServiceOrder):
    return db.query(ServiceOrderTechnicalCompletion).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).first()


def _acceptance(db: Session, order: ServiceOrder):
    return db.query(ServiceOrderCustomerAcceptance).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).first()


def _financially_settled(db: Session, order: ServiceOrder) -> bool:
    plan = payment_plan_projection(db, order, include_release_capability=False)
    if plan:
        if Decimal(str(plan.get("service_outstanding_balance") or 0)) > 0:
            return False
        return all(str(item.get("status", "")).upper() == "PAID" for item in plan.get("installments", []))
    financial = db.query(ServiceOrderFinancial).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).first()
    return not financial or Decimal(str(financial.service_outstanding_balance or 0)) <= 0


def _assert_completion_access(db: Session, order_id: int, actor):
    return scoped_order(db, order_id, actor)


def record_technical_completion(db: Session, order: ServiceOrder, actor, payload: dict[str, Any]):
    _assert_completion_access(db, order.id, actor)
    row = _completion(db, order)
    if row and row.status in {"REVIEWED", "REPORTED"}:
        return row
    if row is None:
        row = ServiceOrderTechnicalCompletion(
            organization_id=order.organization_id,
            service_order_id=order.id,
            responsible_user_id=actor.id,
        )
        db.add(row)
    row.status = "REPORTED"
    row.completion_notes = (payload.get("completion_notes") or payload.get("notes") or "").strip() or None
    row.final_observation = (payload.get("final_observation") or "").strip() or None
    row.evidence_reference = (payload.get("evidence_reference") or "").strip() or None
    row.responsible_user_id = actor.id
    row.technical_completed_at = row.technical_completed_at or _now()
    return row


def review_technical_completion(db: Session, order: ServiceOrder, actor):
    _assert_completion_access(db, order.id, actor)
    row = _completion(db, order)
    if not row:
        raise HTTPException(status_code=409, detail="Conclua tecnicamente o serviço antes da revisão")
    if actor.role not in {"ROOT", "GERENTE", "SUPERVISOR"}:
        raise HTTPException(status_code=403, detail="Sem permissão para revisar esta ordem")
    if actor.role not in {"ROOT", "GERENTE"} and actor.id not in {order.responsible_user_id, order.supervisor_user_id}:
        raise HTTPException(status_code=403, detail="Sem permissão para revisar esta ordem")
    row.status = "REVIEWED"
    row.reviewed_by_user_id = actor.id
    row.reviewed_at = row.reviewed_at or _now()
    return row


def customer_acceptance(db: Session, order: ServiceOrder, *, idempotency_key: str):
    row = _acceptance(db, order)
    if row and row.status == "ACCEPTED":
        return row
    completion = _completion(db, order)
    if not completion:
        raise HTTPException(status_code=409, detail="Conclusão técnica ainda não registrada")
    if not _financially_settled(db, order):
        raise HTTPException(status_code=409, detail="O saldo do serviço ainda não foi quitado")
    if row and row.status == "PROBLEM_REPORTED":
        raise HTTPException(status_code=409, detail="Existe um problema reportado para esta ordem")
    if row is None:
        row = ServiceOrderCustomerAcceptance(
            organization_id=order.organization_id,
            service_order_id=order.id,
            idempotency_key=idempotency_key,
        )
        db.add(row)
    previous_order_status = order.status
    latest_quote = (
        db.query(ServiceOrderQuote)
        .filter_by(
            service_order_id=order.id,
            organization_id=order.organization_id,
            status="APPROVED",
        )
        .order_by(ServiceOrderQuote.version.desc(), ServiceOrderQuote.id.desc())
        .first()
    )
    row.status = "ACCEPTED"
    row.accepted_at = row.accepted_at or _now()
    row.accepted_source = "PUBLIC_TRACKING"
    row.idempotency_key = row.idempotency_key or idempotency_key
    order.status = "COMPLETED"
    order.completed_at = order.completed_at or row.accepted_at
    audit_auth_event(
        db,
        request=None,
        event_type="SERVICE_ORDER_CUSTOMER_ACCEPTED",
        outcome="SUCCESS",
        detail={
            "service_order_id": order.id,
            "organization_id": order.organization_id,
            "source": "PUBLIC_TRACKING",
            "status_before": previous_order_status,
            "status_after": order.status,
            "quote_version": latest_quote.version if latest_quote else None,
            "quote_total": str(latest_quote.approved_total or latest_quote.total) if latest_quote else None,
        },
    )
    warranty = db.query(ServiceOrderWarranty).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).first()
    if warranty is None:
        starts = row.accepted_at
        warranty = ServiceOrderWarranty(
            organization_id=order.organization_id,
            service_order_id=order.id,
            customer_lead_id=order.lead_id,
            warranty_days=order.warranty_days,
            starts_at=starts,
            ends_at=starts + timedelta(days=order.warranty_days),
            scope=completion.final_observation,
        )
        db.add(warranty)
    return row


def customer_report_problem(db: Session, order: ServiceOrder, *, reason: str | None, idempotency_key: str):
    if not _completion(db, order):
        raise HTTPException(status_code=409, detail="Conclusão técnica ainda não registrada")
    row = _acceptance(db, order)
    if row and row.status == "PROBLEM_REPORTED":
        return row
    if row and row.status == "ACCEPTED":
        raise HTTPException(status_code=409, detail="A ordem já foi aceita pelo cliente")
    if row is None:
        row = ServiceOrderCustomerAcceptance(
            organization_id=order.organization_id,
            service_order_id=order.id,
            idempotency_key=idempotency_key,
        )
        db.add(row)
    row.status = "PROBLEM_REPORTED"
    row.problem_reason = (reason or "").strip() or None
    row.problem_reported_at = row.problem_reported_at or _now()
    row.accepted_source = "PUBLIC_TRACKING"
    return row


def completion_projection(db: Session, order: ServiceOrder, *, public: bool = False) -> dict[str, Any]:
    if not _completion_schema_available(db):
        return {
            "technical_complete": False,
            "ready_for_acceptance": False,
            "customer_status": "pending",
            "completed_at": None,
            "warranty": {"active": False, "starts_at": None, "ends_at": None},
        }
    technical = _completion(db, order)
    acceptance = _acceptance(db, order)
    warranty = db.query(ServiceOrderWarranty).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).first()
    settled = _financially_settled(db, order)
    accepted = acceptance and acceptance.status == "ACCEPTED"
    problem = acceptance and acceptance.status == "PROBLEM_REPORTED"
    result = {
        "technical_complete": bool(technical),
        "ready_for_acceptance": bool(technical and settled and not problem and not accepted),
        "customer_status": "accepted" if accepted else "problem_reported" if problem else "pending",
        "completed_at": order.completed_at if accepted else None,
        "warranty": {
            "active": bool(warranty and warranty.status == "ACTIVE"),
            "starts_at": warranty.starts_at if warranty else None,
            "ends_at": warranty.ends_at if warranty else None,
        },
    }
    if technical:
        result["technical"] = {
            "status": technical.status,
            "completion_notes": technical.completion_notes,
            "final_observation": technical.final_observation,
            "technical_completed_at": technical.technical_completed_at,
        }
    if problem:
        result["problem_reason"] = acceptance.problem_reason
    if accepted:
        result["receipt"] = {
            "order_number": order.order_number,
            "completed_at": order.completed_at,
            "currency": "MXN",
            "warranty_ends_at": warranty.ends_at if warranty else None,
        }
    if public:
        safe = {
            "technical_complete": result["technical_complete"],
            "ready_for_acceptance": result["ready_for_acceptance"],
            "customer_status": result["customer_status"],
            "completed_at": result["completed_at"],
            "warranty": result["warranty"],
        }
        if problem:
            safe["problem_reported"] = True
        if accepted:
            safe["receipt"] = result["receipt"]
        return safe
    return result
