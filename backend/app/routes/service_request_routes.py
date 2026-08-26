from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db, require_admin_user, require_root_user
from app.models.lead_event import LeadEvent
from app.models.service_order import ServiceOrder
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.customer_portal_service import service_request_sales_summary
from app.services.service_order_tracking_service import (
    get_tracking_for_actor,
    list_active_tracking_for_actor,
    admin_stop_all_tracking,
    admin_stop_tracking,
    start_tracking,
    stop_tracking,
    stop_tracking_for_order,
    update_tracking_position,
    heartbeat_tracking,
    record_tracking_diagnostic,
    diagnose_tracking_for_root,
    diagnose_tracking_for_root_by_number,
)


router = APIRouter(tags=["service-requests"])


class TriagePayload(BaseModel):
    status: str
    supervisor_user_id: int | None = None


class AssignUserPayload(BaseModel):
    user_id: int


class TrackingStartPayload(BaseModel):
    consent_granted: bool = False


class TrackingPositionPayload(BaseModel):
    latitude: float
    longitude: float
    accuracy_m: float | None = None


class TrackingStopPayload(BaseModel):
    reason: str = "MANUAL"


class AdminTrackingStopPayload(BaseModel):
    reason: str


class TrackingDiagnosticPayload(BaseModel):
    event_type: str
    accuracy_m: float | None = None
    distance_m: float | None = None
    coordinate_changed: bool | None = None


def _actor_label(actor: User) -> str:
    return actor.full_name or actor.username or f"ID {actor.id}"


def _request_for_actor(db: Session, request_id: int, actor: User) -> ServiceRequest:
    service_request = (
        db.query(ServiceRequest)
        .filter(
            ServiceRequest.id == request_id,
            ServiceRequest.organization_id == actor.organization_id,
        )
        .first()
    )
    if not service_request:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    return service_request


def _order_for_actor(db: Session, order_id: int, actor: User) -> ServiceOrder:
    order = (
        db.query(ServiceOrder)
        .filter(
            ServiceOrder.id == order_id,
            ServiceOrder.organization_id == actor.organization_id,
        )
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Orden de servicio no encontrada")
    return order


def _validate_assignee(db: Session, user_id: int, actor: User, allowed_roles: set[str]) -> User:
    user = (
        db.query(User)
        .filter(User.id == user_id, User.organization_id == actor.organization_id)
        .first()
    )
    if not user or user.role not in allowed_roles or not user.is_active or user.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Usuario no disponible para esta asignacion")
    return user


def _add_event(db: Session, order: ServiceOrder, actor: User, event_type: str, message: str):
    db.add(
        LeadEvent(
            organization_id=actor.organization_id,
            lead_id=order.lead_id,
            actor_id=actor.id,
            actor_name=_actor_label(actor),
            event_type=event_type,
            message=message,
        )
    )


@router.get("/sales/service-requests")
def list_sales_service_requests(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    query = db.query(ServiceRequest).filter(ServiceRequest.organization_id == actor.organization_id)
    if status:
        query = query.filter(ServiceRequest.status == status)
    requests = query.order_by(ServiceRequest.created_at.desc()).offset(offset).limit(limit).all()
    return [service_request_sales_summary(service_request) for service_request in requests]


@router.post("/sales/service-requests/{request_id}/triage")
def triage_service_request(
    request_id: int,
    payload: TriagePayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    service_request = _request_for_actor(db, request_id, actor)
    order = service_request.service_order
    if not order:
        raise HTTPException(status_code=409, detail="Solicitud sin OS vinculada")
    status = payload.status.strip().upper()
    if not status:
        raise HTTPException(status_code=400, detail="Status obligatorio")
    if payload.supervisor_user_id:
        supervisor = _validate_assignee(db, payload.supervisor_user_id, actor, {"ROOT", "GERENTE"})
        order.supervisor_user_id = supervisor.id
    service_request.status = status
    service_request.updated_at = datetime.utcnow()
    order.status = status
    order.updated_at = datetime.utcnow()
    _add_event(db, order, actor, "SERVICE_REQUEST_TRIAGED", f"Solicitud revisada: {status}")
    if status in {"CANCELADA", "CANCELLED", "COMPLETED", "CONCLUIDA", "FINALIZADA"}:
        stop_tracking_for_order(
            db,
            order,
            actor=actor,
            reason="CANCELLED" if status in {"CANCELADA", "CANCELLED"} else "COMPLETED",
            preserve_status=True,
        )
    db.commit()
    db.refresh(service_request)
    return service_request_sales_summary(service_request)


@router.post("/service-orders/{order_id}/assign-supervisor")
def assign_service_order_supervisor(
    order_id: int,
    payload: AssignUserPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    order = _order_for_actor(db, order_id, actor)
    supervisor = _validate_assignee(db, payload.user_id, actor, {"ROOT", "GERENTE"})
    order.supervisor_user_id = supervisor.id
    order.updated_at = datetime.utcnow()
    _add_event(db, order, actor, "SERVICE_ORDER_SUPERVISOR_ASSIGNED", f"Supervisor asignado: {supervisor.full_name or supervisor.username}")
    db.commit()
    db.refresh(order)
    return {"id": order.id, "order_number": order.order_number, "supervisor_user_id": order.supervisor_user_id}


@router.post("/service-orders/{order_id}/assign-technician")
def assign_service_order_technician(
    order_id: int,
    payload: AssignUserPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    order = _order_for_actor(db, order_id, actor)
    technician = _validate_assignee(db, payload.user_id, actor, {"BROKER"})
    order.responsible_user_id = technician.id
    order.updated_at = datetime.utcnow()
    if order.lead:
        order.lead.assigned_to_user_id = technician.id
        order.lead.updated_at = datetime.utcnow()
    _add_event(db, order, actor, "SERVICE_ORDER_TECHNICIAN_ASSIGNED", f"Tecnico asignado: {technician.full_name or technician.username}")
    db.commit()
    db.refresh(order)
    return {"id": order.id, "order_number": order.order_number, "responsible_user_id": order.responsible_user_id}


@router.get("/service-orders/tracking/active")
def list_active_service_order_tracking(
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    return {"routes": list_active_tracking_for_actor(db, actor)}


@router.get("/admin/tracking/diagnostic/{service_order_id}")
def admin_tracking_diagnostic(
    service_order_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return diagnose_tracking_for_root(db, service_order_id, actor)


@router.get("/admin/tracking/diagnostic/by-number/{order_number}")
def admin_tracking_diagnostic_by_number(
    order_number: str,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return diagnose_tracking_for_root_by_number(db, order_number, actor)


@router.post("/service-orders/{order_id}/tracking/admin-stop")
def admin_stop_service_order_tracking(
    order_id: int,
    payload: AdminTrackingStopPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    return admin_stop_tracking(db, order_id, actor, payload.reason)


@router.post("/service-orders/tracking/admin-stop-all")
def admin_stop_all_service_order_tracking(
    payload: AdminTrackingStopPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    return {"stopped": admin_stop_all_tracking(db, actor, payload.reason)}


@router.get("/service-orders/{order_id}/tracking")
def get_service_order_tracking(
    order_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    return get_tracking_for_actor(db, order_id, actor)


@router.post("/service-orders/{order_id}/tracking/start")
def start_service_order_tracking(
    order_id: int,
    payload: TrackingStartPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    return start_tracking(db, order_id, actor, payload.consent_granted)


@router.post("/service-orders/{order_id}/tracking/position")
def update_service_order_tracking_position(
    order_id: int,
    payload: TrackingPositionPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    return update_tracking_position(db, order_id, actor, payload.latitude, payload.longitude, payload.accuracy_m)


@router.post("/service-orders/{order_id}/tracking/heartbeat")
def heartbeat_service_order_tracking(
    order_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    return heartbeat_tracking(db, order_id, actor)


@router.post("/service-orders/{order_id}/tracking/stop")
def stop_service_order_tracking(
    order_id: int,
    payload: TrackingStopPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    return stop_tracking(db, order_id, actor, payload.reason)


@router.post("/service-orders/{order_id}/tracking/diagnostic")
def record_service_order_tracking_diagnostic(
    order_id: int,
    payload: TrackingDiagnosticPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    return record_tracking_diagnostic(
        db,
        order_id,
        actor,
        payload.event_type,
        accuracy_m=payload.accuracy_m,
        distance_m=payload.distance_m,
        coordinate_changed=payload.coordinate_changed,
    )
