"""Authorization and lifecycle rules for technician location sharing."""

import math
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lead_event import LeadEvent
from app.models.service_order import ServiceOrder, TRACKING_STARTABLE_ORDER_STATUSES
from app.models.service_order_tracking import ServiceOrderTracking
from app.models.user import User
from app.services.customer_portal_service import public_tracking_url
from app.services.tracking_health_service import tracking_health


STARTABLE_ORDER_STATUSES = TRACKING_STARTABLE_ORDER_STATUSES
TERMINAL_ORDER_STATUSES = {"COMPLETED", "CONCLUIDA", "FINALIZADA", "CANCELLED", "CANCELADA", "PERDIDO"}
TRACKING_DIAGNOSTIC_EVENTS = {
    "GEOLOCATION_PERMISSION_DENIED",
    "GEOLOCATION_UNAVAILABLE",
    "GEOLOCATION_TIMEOUT",
    "PAGE_HIDDEN",
    "PAGE_VISIBLE",
    "NETWORK_OFFLINE",
    "NETWORK_ONLINE",
    "WATCH_RESTARTED",
    "PUBLISH_FAILED",
    "GPS_UPDATE_RECEIVED",
    "GPS_POSITION_PUBLISHED",
}


def _actor_name(actor: User | None) -> str:
    return (actor.full_name or actor.username) if actor else "Sistema"


def _order_for_actor(db: Session, order_id: int, actor: User) -> ServiceOrder:
    query = db.query(ServiceOrder).filter(ServiceOrder.id == order_id)
    if actor.organization_id is not None:
        query = query.filter(ServiceOrder.organization_id == actor.organization_id)
    order = query.first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden de servicio no encontrada")
    return order


def _technician_for_order(db: Session, order: ServiceOrder) -> User | None:
    if not order.responsible_user_id:
        return None
    return db.query(User).filter(User.id == order.responsible_user_id).first()


def _is_visible_to_actor(db: Session, order: ServiceOrder, actor: User) -> bool:
    if actor.role == "ROOT":
        return True
    if actor.role == "BROKER":
        return order.responsible_user_id == actor.id
    if actor.role == "GERENTE":
        technician = _technician_for_order(db, order)
        return bool(order.supervisor_user_id == actor.id or (technician and technician.manager_id == actor.id))
    return False


def _require_assigned_technician(order: ServiceOrder, actor: User):
    if actor.role != "BROKER" or order.responsible_user_id != actor.id:
        raise HTTPException(status_code=403, detail="Somente o tecnico atribuido pode controlar o tracking")


def _validate_position(latitude, longitude, accuracy):
    try:
        lat = float(latitude)
        lng = float(longitude)
        accuracy_value = None if accuracy is None else float(accuracy)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Coordenadas invalidas") from exc
    if not math.isfinite(lat) or not math.isfinite(lng):
        raise HTTPException(status_code=400, detail="Coordenadas invalidas")
    if accuracy_value is not None and (not math.isfinite(accuracy_value) or accuracy_value < 0):
        raise HTTPException(status_code=400, detail="Precisao invalida")
    if not -90 <= lat <= 90:
        raise HTTPException(status_code=400, detail="Latitude invalida")
    if not -180 <= lng <= 180:
        raise HTTPException(status_code=400, detail="Longitude invalida")
    return lat, lng, accuracy_value


def _add_tracking_event(db: Session, order: ServiceOrder, actor: User | None, event_type: str, message: str):
    db.add(LeadEvent(
        organization_id=order.organization_id or (actor.organization_id if actor else None),
        lead_id=order.lead_id,
        actor_id=actor.id if actor else None,
        actor_name=_actor_name(actor),
        event_type=event_type,
        message=message,
    ))


def serialize_tracking(tracking: ServiceOrderTracking | None) -> dict | None:
    if not tracking:
        return None
    health = tracking_health(tracking)
    return {
        "id": tracking.id,
        "service_order_id": tracking.service_order_id,
        "technician_id": tracking.technician_id,
        "tracking_active": tracking.tracking_active,
        "current_lat": tracking.current_lat,
        "current_lng": tracking.current_lng,
        "accuracy_m": tracking.accuracy_m,
        "consent_granted_at": tracking.consent_granted_at,
        "started_at": tracking.started_at,
        "updated_at": tracking.updated_at,
        "last_location_at": health["last_location_at"],
        "seconds_since_last_update": health["seconds_since_last_update"],
        "tracking_health": health["tracking_health"],
        "location_health": health["location_health"],
        "heartbeat_health": health["heartbeat_health"],
        "last_heartbeat_at": health["last_heartbeat_at"],
        "seconds_since_last_heartbeat": health["seconds_since_last_heartbeat"],
        "stopped_at": tracking.stopped_at,
    }


def get_tracking_for_actor(db: Session, order_id: int, actor: User) -> dict:
    order = _order_for_actor(db, order_id, actor)
    if not _is_visible_to_actor(db, order, actor):
        raise HTTPException(status_code=403, detail="Tracking fora da sua estrutura")
    tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(tracking)}


def list_active_tracking_for_actor(db: Session, actor: User) -> list[dict]:
    """Return only active routes visible in the actor's operational scope."""
    if actor.role not in {"ROOT", "GERENTE"} or actor.organization_id is None:
        raise HTTPException(status_code=403, detail="Central de tracking fora do seu escopo")

    rows = (
        db.query(ServiceOrder, ServiceOrderTracking, User)
        .join(ServiceOrderTracking, ServiceOrderTracking.service_order_id == ServiceOrder.id)
        .join(User, User.id == ServiceOrderTracking.technician_id)
        .filter(
            ServiceOrder.organization_id == actor.organization_id,
            ServiceOrderTracking.tracking_active.is_(True),
            ServiceOrder.status == "EN_CAMINO",
        )
        .order_by(ServiceOrderTracking.updated_at.desc(), ServiceOrder.id)
        .all()
    )

    active_routes = []
    for order, tracking, technician in rows:
        if not _is_visible_to_actor(db, order, actor):
            continue
        lead = order.lead
        service_request = order.service_request
        active_routes.append({
            "service_order_id": order.id,
            "order_number": order.order_number,
            "status": order.status,
            "client_name": lead.nome if lead else None,
            "tracking_public_url": public_tracking_url(service_request.tracking_token) if service_request else None,
            "technician": {
                "id": technician.id,
                "name": _actor_name(technician),
            },
            "tracking": serialize_tracking(tracking),
            "service_location": {
                "latitude": order.location_lat,
                "longitude": order.location_lng,
                "accuracy_m": order.location_accuracy_m,
                "source": order.location_source,
                "confirmed_at": order.location_confirmed_at,
            },
        })
    return active_routes


def start_tracking(db: Session, order_id: int, actor: User, consent_granted: bool) -> dict:
    order = _order_for_actor(db, order_id, actor)
    _require_assigned_technician(order, actor)
    if not consent_granted:
        raise HTTPException(status_code=400, detail="Consentimento obrigatorio para compartilhar a localizacao")
    if (order.status or "").upper() in TERMINAL_ORDER_STATUSES:
        raise HTTPException(status_code=409, detail="A OS ja foi encerrada")
    normalized_status = (order.status or "").strip().upper()
    if normalized_status == "EN_CAMINO":
        existing_tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
        if existing_tracking and existing_tracking.tracking_active:
            return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(existing_tracking)}
        raise HTTPException(status_code=409, detail="A OS esta em rota, mas o tracking nao esta ativo")
    if not order.tracking_start_allowed:
        raise HTTPException(status_code=409, detail="A OS nao esta pronta para iniciar a rota")
    now = datetime.utcnow()
    tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    if tracking and tracking.tracking_active:
        return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(tracking)}
    if not tracking:
        tracking = ServiceOrderTracking(service_order_id=order.id, technician_id=actor.id)
        db.add(tracking)
    tracking.technician_id = actor.id
    tracking.tracking_active = True
    tracking.consent_granted_at = now
    tracking.started_at = now
    tracking.updated_at = now
    tracking.last_heartbeat_at = now
    tracking.stopped_at = None
    order.status = "EN_CAMINO"
    order.updated_at = now
    _add_tracking_event(db, order, actor, "TRACKING_STARTED", f"Rota iniciada para OS {order.order_number or order.id}")
    db.commit()
    db.refresh(tracking)
    return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(tracking)}


def heartbeat_tracking(db: Session, order_id: int, actor: User) -> dict:
    order = _order_for_actor(db, order_id, actor)
    _require_assigned_technician(order, actor)
    tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    if not tracking or not tracking.tracking_active:
        raise HTTPException(status_code=409, detail="Tracking nao esta ativo")
    tracking.last_heartbeat_at = datetime.utcnow()
    db.commit()
    db.refresh(tracking)
    return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(tracking)}


def record_tracking_diagnostic(
    db: Session,
    order_id: int,
    actor: User,
    event_type: str,
    *,
    accuracy_m: float | None = None,
    distance_m: float | None = None,
    coordinate_changed: bool | None = None,
) -> dict:
    order = _order_for_actor(db, order_id, actor)
    _require_assigned_technician(order, actor)
    normalized_event = (event_type or "").strip().upper()
    if normalized_event not in TRACKING_DIAGNOSTIC_EVENTS:
        raise HTTPException(status_code=400, detail="Evento de diagnóstico invalido")
    if accuracy_m is not None and (not math.isfinite(float(accuracy_m)) or float(accuracy_m) < 0):
        raise HTTPException(status_code=400, detail="Precisao invalida")
    if distance_m is not None and (not math.isfinite(float(distance_m)) or float(distance_m) < 0):
        raise HTTPException(status_code=400, detail="Distancia invalida")
    tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    if not tracking or not tracking.tracking_active:
        raise HTTPException(status_code=409, detail="Tracking nao esta ativo")
    details = []
    if accuracy_m is not None:
        details.append(f"accuracy_m={float(accuracy_m):.1f}")
    if distance_m is not None:
        details.append(f"distance_m={float(distance_m):.1f}")
    if coordinate_changed is not None:
        details.append(f"changed={'yes' if coordinate_changed else 'no'}")
    suffix = f" ({', '.join(details)})" if details else ""
    _add_tracking_event(db, order, actor, normalized_event, f"Diagnostico de tracking: {normalized_event}{suffix}")
    db.commit()
    return {"recorded": True, "event_type": normalized_event}


def update_tracking_position(db: Session, order_id: int, actor: User, latitude, longitude, accuracy=None) -> dict:
    order = _order_for_actor(db, order_id, actor)
    _require_assigned_technician(order, actor)
    lat, lng, accuracy_value = _validate_position(latitude, longitude, accuracy)
    tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    if not tracking or not tracking.tracking_active:
        raise HTTPException(status_code=409, detail="Tracking nao esta ativo")
    tracking.current_lat = lat
    tracking.current_lng = lng
    tracking.accuracy_m = accuracy_value
    tracking.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(tracking)
    return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(tracking)}


def stop_tracking(db: Session, order_id: int, actor: User, reason: str = "MANUAL") -> dict:
    order = _order_for_actor(db, order_id, actor)
    _require_assigned_technician(order, actor)
    return stop_tracking_for_order(db, order, actor=actor, reason=reason)


def stop_tracking_for_order(
    db: Session,
    order: ServiceOrder,
    *,
    actor: User | None,
    reason: str,
    preserve_status: bool = False,
) -> dict:
    normalized_reason = (reason or "MANUAL").strip().upper()
    if normalized_reason not in {"MANUAL", "ARRIVED", "COMPLETED", "CANCELLED"}:
        raise HTTPException(status_code=400, detail="Motivo de parada invalido")
    tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    now = datetime.utcnow()
    if tracking and tracking.tracking_active:
        tracking.tracking_active = False
        tracking.stopped_at = now
        tracking.updated_at = now
        event_type = "TECHNICIAN_ARRIVED" if normalized_reason == "ARRIVED" else "TRACKING_STOPPED"
        _add_tracking_event(db, order, actor, event_type, f"Tracking encerrado para OS {order.order_number or order.id}: {normalized_reason}")
    if not preserve_status and normalized_reason == "ARRIVED":
        order.status = "EM_ATENDIMENTO"
    elif not preserve_status and normalized_reason == "COMPLETED":
        order.status = "COMPLETED"
        order.completed_at = order.completed_at or now
    elif not preserve_status and normalized_reason == "CANCELLED":
        order.status = "CANCELADA"
    order.updated_at = now
    db.commit()
    if tracking:
        db.refresh(tracking)
    return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(tracking)}
