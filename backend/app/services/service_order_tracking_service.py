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
from app.services.route_intelligence_service import calculate_route
from app.services.tracking_state_service import is_tracking_session_active, tracking_session_state
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


def _is_visible_to_actor(
    db: Session,
    order: ServiceOrder,
    actor: User,
    technician: User | None = None,
) -> bool:
    if actor.role == "ROOT":
        return True
    if actor.organization_id is None or order.organization_id != actor.organization_id:
        return False
    if actor.role == "BROKER":
        return order.responsible_user_id == actor.id
    if actor.role == "GERENTE":
        technician = technician or _technician_for_order(db, order)
        if technician and technician.organization_id != actor.organization_id:
            return False
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


def route_for_order(order: ServiceOrder, tracking: ServiceOrderTracking | None) -> dict:
    if not tracking or not tracking.tracking_active or tracking.current_lat is None or tracking.current_lng is None:
        return {"available": False, "distance_m": None, "duration_s": None, "eta_at": None, "geometry": None}
    if tracking_health(tracking)["tracking_health"] == "OFFLINE":
        return {"available": False, "distance_m": None, "duration_s": None, "eta_at": None, "geometry": None}
    if order.location_lat is None or order.location_lng is None:
        return {"available": False, "distance_m": None, "duration_s": None, "eta_at": None, "geometry": None}
    return calculate_route(
        tracking.current_lat,
        tracking.current_lng,
        order.location_lat,
        order.location_lng,
        cache_key=f"service-order:{order.id}",
    )


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
    return {
        "service_order_id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "tracking": serialize_tracking(tracking),
        "route": route_for_order(order, tracking),
    }


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
        )
        .order_by(ServiceOrderTracking.updated_at.desc(), ServiceOrder.id)
        .all()
    )

    active_routes = []
    for order, tracking, technician in rows:
        if not _is_visible_to_actor(db, order, actor, technician=technician):
            continue
        lead = order.lead
        service_request = order.service_request
        health = tracking_health(tracking)
        session_state = tracking_session_state(order, tracking)
        orphaned = session_state != "ACTIVE" or (
            not order.responsible_user_id
            or order.responsible_user_id != tracking.technician_id
        )
        active_routes.append({
            "service_order_id": order.id,
            "order_number": order.order_number,
            "status": order.status,
            "session_state": "ORPHANED" if orphaned else session_state,
            "tracking_state": "ORPHANED" if orphaned else health["tracking_health"],
            "tracking_health": health["tracking_health"],
            "client_name": lead.nome if lead else None,
            "tracking_public_url": public_tracking_url(service_request.tracking_token) if service_request else None,
            "technician": {
                "id": technician.id,
                "name": _actor_name(technician),
            },
            "tracking": serialize_tracking(tracking),
            "route": route_for_order(order, tracking),
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
    existing_tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    if normalized_status == "EN_CAMINO":
        if existing_tracking and existing_tracking.tracking_active:
            return {"service_order_id": order.id, "order_number": order.order_number, "status": order.status, "tracking": serialize_tracking(existing_tracking)}
        if existing_tracking and existing_tracking.stopped_at is None:
            raise HTTPException(status_code=409, detail="A OS esta em rota, mas o tracking nao esta ativo")
    restarting_stopped_session = bool(existing_tracking and existing_tracking.stopped_at is not None)
    if not order.tracking_start_allowed and not restarting_stopped_session:
        raise HTTPException(status_code=409, detail="A OS nao esta pronta para iniciar a rota")
    now = datetime.utcnow()
    tracking = existing_tracking
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
    return {
        "service_order_id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "tracking": serialize_tracking(tracking),
        "route": route_for_order(order, tracking),
    }


def heartbeat_tracking(db: Session, order_id: int, actor: User) -> dict:
    order = _order_for_actor(db, order_id, actor)
    _require_assigned_technician(order, actor)
    tracking = db.query(ServiceOrderTracking).filter(ServiceOrderTracking.service_order_id == order.id).first()
    if not tracking or not tracking.tracking_active:
        raise HTTPException(status_code=409, detail="Tracking nao esta ativo")
    tracking.last_heartbeat_at = datetime.utcnow()
    db.commit()
    db.refresh(tracking)
    return {
        "service_order_id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "tracking": serialize_tracking(tracking),
        "route": route_for_order(order, tracking),
    }


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
    administrative_reason: str | None = None,
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
        suffix = f" administrative_reason={administrative_reason}" if administrative_reason else ""
        _add_tracking_event(db, order, actor, event_type, f"Tracking encerrado para OS {order.order_number or order.id}: {normalized_reason}{suffix}")
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


ADMIN_STOP_REASONS = {
    "TEST_COMPLETED",
    "ABANDONED_ROUTE",
    "DEVICE_DISCONNECTED",
    "DUPLICATE_ROUTE",
    "OPERATIONAL_CORRECTION",
    "OTHER",
}


def admin_stop_tracking(db: Session, order_id: int, actor: User, reason: str) -> dict:
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Somente root ou gerente pode encerrar rotas administrativamente")
    normalized_reason = (reason or "").strip().upper()
    if normalized_reason not in ADMIN_STOP_REASONS:
        raise HTTPException(status_code=400, detail="Motivo administrativo invalido")
    order = _order_for_actor(db, order_id, actor)
    if not _is_visible_to_actor(db, order, actor):
        raise HTTPException(status_code=403, detail="Tracking fora da sua estrutura")
    return stop_tracking_for_order(
        db,
        order,
        actor=actor,
        reason="MANUAL",
        preserve_status=True,
        administrative_reason=normalized_reason,
    )


def admin_stop_all_tracking(db: Session, actor: User, reason: str) -> list[dict]:
    if actor.role != "ROOT":
        raise HTTPException(status_code=403, detail="Somente root pode encerrar todas as rotas")
    normalized_reason = (reason or "").strip().upper()
    if normalized_reason not in ADMIN_STOP_REASONS:
        raise HTTPException(status_code=400, detail="Motivo administrativo invalido")
    routes = list_active_tracking_for_actor(db, actor)
    results = []
    for route in routes:
        results.append(admin_stop_tracking(db, route["service_order_id"], actor, normalized_reason))
    return results


def diagnose_tracking_for_root(db: Session, order_id: int, actor: User) -> dict:
    """Build a read-only comparison of backend and public tracking projections."""
    if actor.role != "ROOT":
        raise HTTPException(status_code=403, detail="Somente root pode consultar este diagnostico")
    order = _order_for_actor(db, order_id, actor)
    tracking_records = (
        db.query(ServiceOrderTracking)
        .filter(ServiceOrderTracking.service_order_id == order.id)
        .order_by(ServiceOrderTracking.id)
        .all()
    )
    tracking = order.tracking
    session_state = tracking_session_state(order, tracking)
    health = tracking_health(tracking)
    technician = (
        db.query(User).filter(User.id == tracking.technician_id).first()
        if tracking
        else None
    )
    technician_name = _actor_name(technician) if technician else None
    route = route_for_order(order, tracking)

    # Import locally because the tracking service already owns the public URL helper.
    from app.services.customer_portal_service import service_request_public_tracking

    public_projection = service_request_public_tracking(order.service_request) if order.service_request else None
    if public_projection is not None:
        public_projection = dict(public_projection)
        public_projection.pop("tracking_token", None)
    inconsistencies = []
    if tracking and tracking.tracking_active and tracking.stopped_at is not None:
        inconsistencies.append("ACTIVE_WITH_STOPPED_AT")
    if tracking and tracking.tracking_active and technician is None:
        inconsistencies.append("ACTIVE_WITHOUT_TECHNICIAN")
    if tracking and tracking.tracking_active and (tracking.current_lat is None or tracking.current_lng is None):
        inconsistencies.append("ACTIVE_WITHOUT_COORDINATES")
    if len(tracking_records) > 1:
        inconsistencies.append("MULTIPLE_TRACKING_RECORDS")
    if public_projection is not None:
        if public_projection["tracking_active"] != bool(tracking and tracking.tracking_active):
            inconsistencies.append("PUBLIC_ADMIN_ACTIVE_MISMATCH")
        if is_tracking_session_active(order, tracking) and not public_projection.get("technician_display_name"):
            inconsistencies.append("PUBLIC_MISSING_TECHNICIAN_NAME")
        if is_tracking_session_active(order, tracking) and not public_projection.get("last_location_updated_at"):
            inconsistencies.append("PUBLIC_MISSING_LOCATION_TIMESTAMP")
    if (
        tracking
        and tracking.updated_at
        and health["seconds_since_last_update"] is not None
        and health["seconds_since_last_update"] <= 120
        and health["tracking_health"] == "OFFLINE"
    ):
        inconsistencies.append("RECENT_POSITION_BUT_OFFLINE")
    if tracking and session_state == "ORPHANED":
        inconsistencies.append("ORDER_STATUS_TRACKING_MISMATCH")

    return {
        "service_order": {
            "id": order.id,
            "number": order.order_number,
            "status": order.status,
            "organization_id": order.organization_id,
            "responsible_user_id": order.responsible_user_id,
            "supervisor_user_id": order.supervisor_user_id,
        },
        "tracking": {
            "id": tracking.id if tracking else None,
            "tracking_active": tracking.tracking_active if tracking else False,
            "stopped_at": tracking.stopped_at if tracking else None,
            "updated_at": tracking.updated_at if tracking else None,
            "last_location_at": health["last_location_at"],
            "last_heartbeat_at": health["last_heartbeat_at"],
            "technician_id": tracking.technician_id if tracking else None,
            "latitude": tracking.current_lat if tracking else None,
            "longitude": tracking.current_lng if tracking else None,
        },
        "canonical": {
            "session_state": session_state,
            "tracking_health": health["tracking_health"],
        },
        "technician": {
            "id": technician.id if technician else None,
            "display_name": technician_name,
            "organization_id": technician.organization_id if technician else None,
            "manager_id": technician.manager_id if technician else None,
        },
        "scope": {
            "actor_organization_id": actor.organization_id,
            "organization_matches_order": bool(actor.organization_id and actor.organization_id == order.organization_id),
            "organization_matches_tracking_technician": bool(
                technician and actor.organization_id and technician.organization_id == actor.organization_id
            ),
            "tracking_technician_matches_order_responsible": bool(
                tracking and order.responsible_user_id == tracking.technician_id
            ),
            "tracking_technician_is_under_actor": bool(
                actor.role == "ROOT"
                or (technician and technician.manager_id == actor.id)
                or order.supervisor_user_id == actor.id
            ),
            "order_supervisor_matches_actor": order.supervisor_user_id == actor.id,
        },
        "destination": {
            "latitude": order.location_lat,
            "longitude": order.location_lng,
        },
        "route": {
            "available": bool(route.get("available")),
            "distance_m": route.get("distance_m"),
            "duration_s": route.get("duration_s"),
            "eta_at": route.get("eta_at"),
            "geometry_present": bool(route.get("geometry")),
        },
        "public_projection": public_projection,
        "tracking_records": [
            {
                "id": item.id,
                "tracking_active": item.tracking_active,
                "stopped_at": item.stopped_at,
                "technician_id": item.technician_id,
                "updated_at": item.updated_at,
                "last_location_at": tracking_health(item)["last_location_at"],
                "last_heartbeat_at": item.last_heartbeat_at,
            }
            for item in tracking_records
        ],
        "tracking_record_count": len(tracking_records),
        "tracking_service_order_unique_constraint": bool(ServiceOrderTracking.__table__.c.service_order_id.unique),
        "inconsistencies": sorted(set(inconsistencies)),
    }


def diagnose_tracking_for_root_by_number(db: Session, order_number: str, actor: User) -> dict:
    """Resolve the public order number, then reuse the scoped ROOT diagnostic."""
    if actor.role != "ROOT":
        raise HTTPException(status_code=403, detail="Somente root pode consultar este diagnostico")
    normalized_number = (order_number or "").strip()
    order = (
        db.query(ServiceOrder)
        .filter(ServiceOrder.order_number == normalized_number)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Orden de servicio no encontrada")
    return diagnose_tracking_for_root(db, order.id, actor)
