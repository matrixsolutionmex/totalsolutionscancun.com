import math
import os
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.service_order import ServiceOrder
from app.models.service_order_tracking import ServiceOrderTracking
from app.models.service_order_warranty_claim import ServiceOrderWarrantyClaim
from app.models.service_order_review import ServiceOrderReview
from app.models.technician_skill import TechnicianSkill
from app.models.user import User
from app.services.service_order_review_service import COMPLETED_STATUSES, _aggregate


BUSY_STATUSES = {"EN_CAMINO", "EM_ATENDIMENTO", "ATENDIMENTO", "IN_PROGRESS", "EM_EXECUCAO", "EN_EJECUCION"}
TERMINAL_STATUSES = {"COMPLETED", "CONCLUIDA", "FINALIZADA", "CANCELLED", "CANCELADA", "PERDIDO"}
DEFAULT_WEIGHTS = {
    "skill": 50.0,
    "availability": 20.0,
    "distance": 15.0,
    "workload": 10.0,
    "quality": 5.0,
}


def _weight(name: str) -> float:
    raw = os.getenv(f"TECHNICIAN_RECOMMENDATION_WEIGHT_{name.upper()}")
    try:
        return max(float(raw), 0.0) if raw is not None else DEFAULT_WEIGHTS[name]
    except ValueError:
        return DEFAULT_WEIGHTS[name]


def _normalize(value: str | None) -> str:
    return " ".join((value or "").strip().upper().replace("_", " ").split())


def _coordinates(order: ServiceOrder) -> tuple[float, float] | None:
    lat = order.location_lat
    lng = order.location_lng
    if (lat is None or lng is None) and order.service_request:
        lat, lng = order.service_request.location_lat, order.service_request.location_lng
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    return (lat, lng) if math.isfinite(lat) and math.isfinite(lng) else None


def _distance_km(origin: tuple[float, float] | None, destination: tuple[float, float] | None) -> float | None:
    if not origin or not destination:
        return None
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a))), 2)


def _quality(db: Session, organization_id: int, technician_id: int) -> dict[str, Any]:
    return _aggregate(db, organization_id, technician_id)


def _candidate_state(db: Session, order: ServiceOrder, technician: User) -> dict[str, Any]:
    tracking = db.query(ServiceOrderTracking).filter(
        ServiceOrderTracking.technician_id == technician.id,
        ServiceOrderTracking.tracking_active.is_(True),
    ).order_by(ServiceOrderTracking.updated_at.desc()).first()
    tech_coordinates = (tracking.current_lat, tracking.current_lng) if tracking and tracking.current_lat is not None and tracking.current_lng is not None else None
    distance = _distance_km(tech_coordinates, _coordinates(order))
    active_orders = db.query(ServiceOrder).filter(
        ServiceOrder.organization_id == order.organization_id,
        ServiceOrder.responsible_user_id == technician.id,
        ~ServiceOrder.status.in_(TERMINAL_STATUSES),
        ServiceOrder.id != order.id,
    ).all()
    busy = any(_normalize(row.status) in {_normalize(value) for value in BUSY_STATUSES} for row in active_orders)
    future_conflict = any(row.scheduled_at and row.scheduled_at >= datetime.utcnow() for row in active_orders)
    availability = "BUSY" if busy or future_conflict else "UNKNOWN"
    quality = _quality(db, order.organization_id, technician.id)
    skill = _normalize(order.service_request.service_category if order.service_request else None)
    skills = {_normalize(row.skill) for row in db.query(TechnicianSkill).filter_by(
        organization_id=order.organization_id, technician_user_id=technician.id, active=True,
    ).all()}
    skill_match = "STRONG" if skill and skill in skills else "INCOMPATIBLE" if skills else "UNKNOWN"
    if skill_match == "STRONG":
        skill_score = _weight("skill")
    else:
        skill_score = 0.0
    availability_score = 0.0 if availability == "BUSY" else _weight("availability")
    distance_score = _weight("distance") if distance is not None else 0.0
    workload_score = max(0.0, _weight("workload") - min(len(active_orders), 5) * (_weight("workload") / 5))
    quality_score = _weight("quality") if quality["reviews_total"] else 0.0
    reasons = []
    reasons.append(
        "Habilidade compatível"
        if skill_match == "STRONG"
        else "Habilidade incompatível"
        if skill_match == "INCOMPATIBLE"
        else "Habilidade não cadastrada; confirmar manualmente"
    )
    reasons.append("Disponibilidade não confirmada" if availability == "UNKNOWN" else "Já possui carga operacional")
    reasons.append(f"Distância estimada: {distance} km" if distance is not None else "Distância: N/A (sem coordenadas reais)")
    reasons.append(f"Carga ativa: {len(active_orders)}")
    reasons.append("Qualidade com amostra disponível" if quality["reviews_total"] else "Qualidade: amostra insuficiente")
    return {
        "eligible": availability != "BUSY" and skill_match != "INCOMPATIBLE",
        "technician": {"id": technician.id, "display_name": technician.full_name or technician.username},
        "skill_match": skill_match,
        "availability": availability,
        "distance_km": distance,
        "workload": {"active_orders": len(active_orders), "future_schedule_conflict": future_conflict},
        "quality": {
            "completed_services": quality["services_completed"],
            "review_count": quality["reviews_total"],
            "average_rating": quality["average_rating"],
            "nps": quality["nps"],
            "claim_rate": quality["warranty_claim_rate"],
            "confidence": "LOW" if quality["reviews_total"] < 3 else "MEDIUM" if quality["reviews_total"] < 10 else "HIGH",
        },
        "score": round(skill_score + availability_score + distance_score + workload_score + quality_score, 2),
        "reasons": reasons,
    }


def recommend_technicians(db: Session, order_id: int, actor: User) -> dict[str, Any]:
    if actor.role not in {"ROOT", "GERENTE"} or not actor.organization_id:
        raise HTTPException(status_code=403, detail="Somente administradores operacionais podem recomendar técnicos")
    order_query = db.query(ServiceOrder).filter(ServiceOrder.id == order_id)
    if actor.role != "ROOT":
        order_query = order_query.filter(ServiceOrder.organization_id == actor.organization_id)
    order = order_query.first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden de servicio no encontrada")
    query = db.query(User).filter(User.organization_id == order.organization_id, User.role == "BROKER", User.is_active.is_(True), User.status == "ACTIVE")
    if actor.role == "GERENTE":
        query = query.filter(User.manager_id == actor.id)
    candidates = [_candidate_state(db, order, technician) for technician in query.order_by(User.full_name, User.id).all()]
    eligible = [item for item in candidates if item["eligible"]]
    excluded = [item for item in candidates if not item["eligible"]]
    eligible.sort(key=lambda item: (-item["score"], item["technician"]["id"]))
    return {"service_order_id": order.id, "order_number": order.order_number, "service_category": order.service_request.service_category if order.service_request else None, "candidates": eligible, "excluded": excluded, "scoring": {"weights": {name: _weight(name) for name in DEFAULT_WEIGHTS}, "explainable": True}}


def validate_recommended_assignment(db: Session, order: ServiceOrder, technician_id: int, actor: User) -> User:
    technician = db.query(User).filter(User.id == technician_id, User.organization_id == order.organization_id).first()
    if not technician or technician.role != "BROKER" or not technician.is_active or technician.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Tecnico nao elegivel para esta organizacao")
    if actor.role == "GERENTE" and technician.manager_id != actor.id:
        raise HTTPException(status_code=403, detail="Tecnico fora da sua hierarquia")
    state = _candidate_state(db, order, technician)
    if not state["eligible"]:
        raise HTTPException(status_code=409, detail="Tecnico indisponivel para esta atribuicao")
    return technician
