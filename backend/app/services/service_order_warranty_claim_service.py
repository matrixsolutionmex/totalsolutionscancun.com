from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.auth_security import audit_auth_event
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderWarranty
from app.models.service_order_warranty_claim import ServiceOrderWarrantyClaim, ServiceOrderWarrantyClaimEvent
from app.services.service_order_quote_service import scoped_order

ACTIVE_STATUSES = {"OPEN", "UNDER_REVIEW", "APPROVED", "TECHNICIAN_ASSIGNED", "IN_PROGRESS", "RESOLVED"}
PUBLIC_STATUS = {
    "OPEN": "open",
    "UNDER_REVIEW": "under_review",
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "TECHNICIAN_ASSIGNED": "technician_assigned",
    "IN_PROGRESS": "in_progress",
    "RESOLVED": "resolved",
    "CLOSED": "closed",
}
REVIEWABLE_STATUSES = {"OPEN", "UNDER_REVIEW"}


def _now():
    return datetime.utcnow()


def _warranty(db: Session, order: ServiceOrder) -> ServiceOrderWarranty:
    warranty = db.query(ServiceOrderWarranty).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).first()
    if not warranty or warranty.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="A garantia desta ordem não está ativa")
    if warranty.ends_at < _now():
        raise HTTPException(status_code=409, detail="O prazo de garantia expirou")
    if order.status not in {"COMPLETED", "CONCLUIDA", "FINALIZADO", "FINALIZADA"}:
        raise HTTPException(status_code=409, detail="A garantia só está disponível após a conclusão")
    return warranty


def _event(db, claim, event_type, *, actor_id=None, source="INTERNAL", notes=None):
    db.add(ServiceOrderWarrantyClaimEvent(
        organization_id=claim.organization_id,
        claim_id=claim.id,
        event_type=event_type,
        actor_user_id=actor_id,
        source=source,
        notes=notes,
    ))


def _assert_claim_access(db: Session, claim_id: int, actor):
    claim = db.query(ServiceOrderWarrantyClaim).filter_by(id=claim_id, organization_id=actor.organization_id).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Chamado de garantia não encontrado")
    if getattr(actor, "id", None) == claim.assigned_user_id:
        order = db.query(ServiceOrder).filter_by(id=claim.service_order_id, organization_id=actor.organization_id).first()
        if order:
            return claim, order
    order = scoped_order(db, claim.service_order_id, actor)
    return claim, order


def create_claim(db: Session, order: ServiceOrder, *, reason: str, idempotency_key: str,
                 description: str | None = None, evidence_reference: str | None = None,
                 actor=None, source="PUBLIC_TRACKING"):
    warranty = _warranty(db, order)
    existing = db.query(ServiceOrderWarrantyClaim).filter_by(idempotency_key=idempotency_key).first()
    if existing:
        return existing
    open_claim = db.query(ServiceOrderWarrantyClaim).filter(
        ServiceOrderWarrantyClaim.warranty_id == warranty.id,
        ServiceOrderWarrantyClaim.status.in_(ACTIVE_STATUSES),
    ).first()
    if open_claim:
        return open_claim
    claim = ServiceOrderWarrantyClaim(
        organization_id=order.organization_id,
        warranty_id=warranty.id,
        service_order_id=order.id,
        customer_lead_id=order.lead_id,
        reason=reason.strip(),
        description=(description or "").strip() or None,
        evidence_reference=(evidence_reference or "").strip() or None,
        created_by_user_id=getattr(actor, "id", None),
        idempotency_key=idempotency_key,
    )
    if not claim.reason:
        raise HTTPException(status_code=422, detail="Informe o motivo da garantia")
    db.add(claim)
    db.flush()
    _event(db, claim, "CREATED", actor_id=getattr(actor, "id", None), source=source)
    audit_auth_event(db, request=None, event_type="WARRANTY_CLAIM_CREATED", outcome="SUCCESS", detail={
        "claim_id": claim.id, "service_order_id": order.id, "organization_id": order.organization_id, "source": source,
    })
    return claim


def _set_status(db, claim, status, *, actor, source="INTERNAL", notes=None):
    if claim.status == status:
        return claim
    claim.status = status
    claim.reviewed_by_user_id = getattr(actor, "id", None) if status in {"APPROVED", "REJECTED"} else claim.reviewed_by_user_id
    claim.review_notes = notes if status in {"APPROVED", "REJECTED"} else claim.review_notes
    now = _now()
    if status == "UNDER_REVIEW":
        claim.first_response_at = claim.first_response_at or now
    if status == "TECHNICIAN_ASSIGNED":
        claim.assigned_at = claim.assigned_at or now
    if status == "RESOLVED":
        claim.resolved_at = claim.resolved_at or now
    if status == "CLOSED":
        claim.closed_at = claim.closed_at or now
    _event(db, claim, status, actor_id=getattr(actor, "id", None), source=source, notes=notes)
    return claim


def review_claim(db: Session, claim_id: int, actor, *, approve: bool, notes=None):
    claim, _ = _assert_claim_access(db, claim_id, actor)
    if actor.role not in {"ROOT", "GERENTE", "SUPERVISOR"}:
        raise HTTPException(status_code=403, detail="Sem permissão para revisar garantia")
    if actor.role == "SUPERVISOR" and actor.id not in {claim.created_by_user_id}:
        # Supervisor access remains organization-scoped through scoped_order; this only prevents global review.
        order = db.query(ServiceOrder).filter_by(id=claim.service_order_id).first()
        if not order or actor.id not in {order.supervisor_user_id, order.responsible_user_id}:
            raise HTTPException(status_code=403, detail="Sem permissão para revisar esta garantia")
    if claim.status == "APPROVED" and approve:
        return claim
    if claim.status == "REJECTED" and not approve:
        return claim
    if claim.status not in REVIEWABLE_STATUSES:
        raise HTTPException(status_code=409, detail="O chamado não está disponível para revisão")
    return _set_status(db, claim, "APPROVED" if approve else "REJECTED", actor=actor, notes=notes)


def assign_claim(db: Session, claim_id: int, actor, technician_id: int):
    claim, order = _assert_claim_access(db, claim_id, actor)
    if actor.role not in {"ROOT", "GERENTE", "SUPERVISOR"}:
        raise HTTPException(status_code=403, detail="Sem permissão para atribuir garantia")
    technician = db.query(type(actor)).filter_by(id=technician_id, organization_id=actor.organization_id).first()
    if not technician or technician.role not in {"BROKER", "TECNICO", "TÉCNICO"}:
        raise HTTPException(status_code=404, detail="Técnico não encontrado na organização")
    if actor.role == "SUPERVISOR" and technician.manager_id != actor.id:
        raise HTTPException(status_code=403, detail="Técnico fora da sua hierarquia")
    if claim.status not in {"APPROVED", "TECHNICIAN_ASSIGNED"}:
        raise HTTPException(status_code=409, detail="O chamado precisa ser aprovado antes da atribuição")
    claim.assigned_user_id = technician.id
    return _set_status(db, claim, "TECHNICIAN_ASSIGNED", actor=actor, notes=f"assigned_user_id={technician.id}")


def update_claim(db: Session, claim_id: int, actor, *, status: str, notes=None):
    claim, _ = _assert_claim_access(db, claim_id, actor)
    if actor.role not in {"ROOT", "GERENTE", "SUPERVISOR"} and actor.id != claim.assigned_user_id:
        raise HTTPException(status_code=403, detail="Sem permissão para atualizar esta garantia")
    if status not in {"IN_PROGRESS", "RESOLVED"}:
        raise HTTPException(status_code=422, detail="Transição de garantia inválida")
    if status == "IN_PROGRESS" and claim.status not in {"TECHNICIAN_ASSIGNED", "IN_PROGRESS"}:
        raise HTTPException(status_code=409, detail="O chamado ainda não está atribuído")
    if status == "RESOLVED" and claim.status not in {"IN_PROGRESS", "RESOLVED"}:
        raise HTTPException(status_code=409, detail="O chamado ainda não está em atendimento")
    if status == "RESOLVED" and not claim.assigned_user_id and actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=409, detail="A garantia precisa de um técnico atribuído")
    claim.resolution_notes = (notes or "").strip() or claim.resolution_notes
    return _set_status(db, claim, status, actor=actor, notes=notes)


def customer_confirm(db: Session, order: ServiceOrder, claim_id: int, *, problem: bool, notes=None):
    claim = db.query(ServiceOrderWarrantyClaim).filter_by(
        id=claim_id, service_order_id=order.id, organization_id=order.organization_id
    ).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Chamado de garantia não encontrado")
    if claim.status == "CLOSED" and not problem and claim.customer_confirmation_status == "CONFIRMED":
        return claim
    if claim.status != "RESOLVED":
        raise HTTPException(status_code=409, detail="O chamado ainda não está resolvido")
    claim.customer_confirmation_status = "PROBLEM_REPORTED" if problem else "CONFIRMED"
    claim.customer_problem_notes = (notes or "").strip() or None
    now = _now()
    if problem:
        claim.customer_problem_reported_at = claim.customer_problem_reported_at or now
        return _set_status(db, claim, "IN_PROGRESS", actor=None, source="PUBLIC_TRACKING", notes=notes)
    claim.customer_confirmed_at = claim.customer_confirmed_at or now
    return _set_status(db, claim, "CLOSED", actor=None, source="PUBLIC_TRACKING")


def claim_payload(claim, *, public=False):
    result = {
        "id": claim.id,
        "status": PUBLIC_STATUS.get(claim.status, "open") if public else claim.status,
        "reason": claim.reason,
        "description": claim.description,
        "evidence_reference": claim.evidence_reference,
        "created_at": claim.created_at,
        "updated_at": claim.updated_at,
        "resolved_at": claim.resolved_at,
        "closed_at": claim.closed_at,
        "customer_confirmation_status": claim.customer_confirmation_status,
    }
    if not public:
        result.update({"assigned_user_id": claim.assigned_user_id, "organization_id": claim.organization_id, "service_order_id": claim.service_order_id,
                       "warranty_id": claim.warranty_id, "review_notes": claim.review_notes,
                       "resolution_notes": claim.resolution_notes, "created_by_user_id": claim.created_by_user_id})
    return result


def claims_projection(db: Session, order: ServiceOrder, *, public=False):
    if not inspect(db.get_bind()).has_table(ServiceOrderWarrantyClaim.__tablename__):
        return []
    rows = db.query(ServiceOrderWarrantyClaim).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).order_by(ServiceOrderWarrantyClaim.created_at.asc()).all()
    return [claim_payload(row, public=public) for row in rows]
