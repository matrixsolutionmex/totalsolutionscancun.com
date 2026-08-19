"""Matching, privacy filtering and atomic claims for ServiceOpportunity."""

from datetime import datetime
from math import asin, cos, radians, sin, sqrt
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.lead_event import LeadEvent
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.schemas.lead_schema import LeadCreate
from app.services.lead_creation_service import create_lead_record
from app.services.pablo_location_service import get_active_location
from app.services.service_order_service import ensure_service_order


AVAILABLE = "AVAILABLE"
CLAIMED = "CLAIMED"
MARKETPLACE = "MARKETPLACE"


def haversine_km(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    radius = 6371.0
    d_lat = radians(latitude_b - latitude_a)
    d_lon = radians(longitude_b - longitude_a)
    value = sin(d_lat / 2) ** 2 + cos(radians(latitude_a)) * cos(radians(latitude_b)) * sin(d_lon / 2) ** 2
    return radius * 2 * asin(sqrt(value))


def _scope_query(db: Session, actor: User):
    return db.query(ServiceOpportunity).filter(
        ServiceOpportunity.organization_id == actor.organization_id,
        ServiceOpportunity.source == MARKETPLACE,
        ServiceOpportunity.status == AVAILABLE,
    )


def _distance(location: dict | None, opportunity: ServiceOpportunity) -> float | None:
    if not location or opportunity.approx_latitude is None or opportunity.approx_longitude is None:
        return None
    return round(haversine_km(location["latitude"], location["longitude"], opportunity.approx_latitude, opportunity.approx_longitude), 1)


def _score(opportunity: ServiceOpportunity, distance_km: float | None) -> float:
    urgency = {"EMERGENCIA": 40, "CRITICA": 40, "ALTA": 25, "MEDIA": 12, "NORMAL": 4, "BAIXA": 1}
    geographic = max(0, 40 - distance_km * 3) if distance_km is not None else 0
    return geographic + urgency.get((opportunity.urgency or "NORMAL").upper(), 4)


def _value_range(opportunity: ServiceOpportunity) -> str | None:
    low, high = opportunity.estimated_value_min, opportunity.estimated_value_max
    if low is None and high is None:
        return None
    if low is None:
        return f"até {high}"
    if high is None:
        return f"a partir de {low}"
    return f"{low} – {high}"


def public_opportunity(opportunity: ServiceOpportunity, *, distance_km: float | None = None) -> dict:
    """Return only pre-claim operational information. No lead/contact/address data."""
    return {
        "public_id": opportunity.public_id,
        "service_type": opportunity.service_type,
        "segment": opportunity.segment,
        "country": opportunity.country,
        "state": opportunity.state,
        "city": opportunity.city,
        "distance_km": distance_km,
        "urgency": opportunity.urgency,
        "estimated_value_range": _value_range(opportunity),
        "scheduled_for": opportunity.scheduled_for.isoformat() if opportunity.scheduled_for else None,
        "description": opportunity.description_public,
        "status": opportunity.status,
    }


def private_opportunity(db: Session, opportunity: ServiceOpportunity, actor: User) -> dict:
    lead = db.query(Lead).filter(Lead.id == opportunity.lead_id, Lead.organization_id == actor.organization_id).first()
    order = db.query(ServiceOrder).filter(ServiceOrder.id == opportunity.service_order_id).first() if opportunity.service_order_id else None
    return {
        **public_opportunity(opportunity),
        "status": opportunity.status,
        "client": {"name": lead.nome, "phone": lead.contato, "email": lead.email, "address": lead.endereco} if lead else None,
        "service_order": {"id": order.id, "order_number": order.order_number, "status": order.status} if order else None,
    }


def list_opportunities(db: Session, actor: User, *, service: str | None = None, city: str | None = None,
                       state: str | None = None, country: str | None = None, urgency: str | None = None,
                       max_distance: float | None = None, sort: str = "distance") -> list[dict]:
    query = _scope_query(db, actor)
    if service:
        query = query.filter(or_(ServiceOpportunity.service_type.ilike(f"%{service.strip()}%"), ServiceOpportunity.segment.ilike(f"%{service.strip()}%")))
    if city:
        query = query.filter(ServiceOpportunity.city.ilike(city.strip()))
    if state:
        query = query.filter(ServiceOpportunity.state.ilike(state.strip()))
    if country:
        query = query.filter(ServiceOpportunity.country.ilike(country.strip()))
    if urgency:
        query = query.filter(ServiceOpportunity.urgency.ilike(urgency.strip()))
    location = get_active_location(actor)
    result = []
    for opportunity in query.limit(200).all():
        distance = _distance(location, opportunity)
        if max_distance is not None and (distance is None or distance > max_distance):
            continue
        item = public_opportunity(opportunity, distance_km=distance)
        item["match_score"] = round(_score(opportunity, distance), 2)
        result.append(item)
    if sort == "urgency":
        result.sort(key=lambda item: {"EMERGENCIA": 0, "CRITICA": 0, "ALTA": 1, "MEDIA": 2, "NORMAL": 3}.get(item["urgency"], 4))
    elif sort == "value":
        result.sort(key=lambda item: item["estimated_value_range"] or "", reverse=True)
    elif sort == "recent":
        result.sort(key=lambda item: item["scheduled_for"] or "", reverse=True)
    else:
        result.sort(key=lambda item: (item["distance_km"] is None, item["distance_km"] or 999999, -item["match_score"]))
    return result


def marketplace_query_reply(db: Session, actor: User, message: str) -> str | None:
    normalized = message.lower()
    if not any(term in normalized for term in ("serviço", "servico", "service", "atendimento", "oportunidade")):
        return None
    if not any(term in normalized for term in ("perto", "próximo", "proximo", "near", "disponível", "disponivel", "urgente")):
        return None
    sort = "urgency" if any(term in normalized for term in ("urgente", "urgency", "urgência", "urgencia")) else "distance"
    items = list_opportunities(db, actor, sort=sort)[:5]
    if not items:
        return "Não encontrei oportunidades disponíveis dentro do seu escopo."
    rows = [f"{item['service_type']} em {item['city'] or 'região não informada'}" + (f", {item['distance_km']} km" if item["distance_km"] is not None else "") + f", urgência {item['urgency']}" for item in items]
    return "Encontrei estas oportunidades disponíveis:\n" + "\n".join(f"- {row}" for row in rows)


def create_opportunity_from_service_request(db: Session, service_request: ServiceRequest) -> ServiceOpportunity:
    existing = db.query(ServiceOpportunity).filter(ServiceOpportunity.service_request_id == service_request.id).first()
    if existing:
        return existing
    property_record = service_request.property_record
    def approximate(value):
        try:
            return round(float(value), 2) if value is not None else None
        except (TypeError, ValueError):
            return None
    description = (service_request.problem_description or "").strip()
    description = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[contato removido]", description)
    description = re.sub(r"(?:\+?\d[\d ()-]{7,})", "[telefone removido]", description)[:500]
    opportunity = ServiceOpportunity(
        public_id=f"MKT-{uuid4().hex[:12].upper()}", organization_id=service_request.organization_id,
        source=MARKETPLACE, service_type=service_request.service_category, segment=service_request.service_category,
        country=property_record.country_code if property_record else None,
        state=property_record.administrative_area if property_record else None,
        city=property_record.locality if property_record else None,
        approx_latitude=approximate(property_record.latitude if property_record else None),
        approx_longitude=approximate(property_record.longitude if property_record else None),
        urgency=service_request.urgency, scheduled_for=service_request.preferred_visit_at,
        description_public=description, status=AVAILABLE, lead_id=service_request.lead_id,
        service_order_id=service_request.service_order.id if service_request.service_order else None,
        service_request_id=service_request.id,
    )
    db.add(opportunity)
    db.add(LeadEvent(organization_id=service_request.organization_id, lead_id=service_request.lead_id,
                     actor_id=None, actor_name="Sistema", event_type="MARKETPLACE_OPPORTUNITY_CREATED",
                     message="Oportunidade de marketplace criada a partir de solicitação de serviço"))
    db.flush()
    return opportunity


def get_available_opportunity(db: Session, actor: User, public_id: str) -> ServiceOpportunity:
    opportunity = _scope_query(db, actor).filter(ServiceOpportunity.public_id == public_id).first()
    if not opportunity:
        raise HTTPException(status_code=404, detail="Oportunidade não disponível")
    return opportunity


def claim_opportunity(db: Session, actor: User, public_id: str) -> dict:
    if actor.role != "BROKER" or not actor.is_active or actor.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Apenas técnicos ativos podem aceitar oportunidades")
    opportunity = db.query(ServiceOpportunity).filter(
        ServiceOpportunity.organization_id == actor.organization_id,
        ServiceOpportunity.source == MARKETPLACE,
        ServiceOpportunity.public_id == public_id,
    ).first()
    if not opportunity:
        raise HTTPException(status_code=404, detail="Oportunidade não encontrada")
    if opportunity.status != AVAILABLE:
        raise HTTPException(status_code=409, detail="Esta oportunidade acabou de ser aceita por outro profissional.")
    now = datetime.utcnow()
    claimed = db.query(ServiceOpportunity).filter(
        ServiceOpportunity.id == opportunity.id,
        ServiceOpportunity.organization_id == actor.organization_id,
        ServiceOpportunity.status == AVAILABLE,
    ).update({"status": CLAIMED, "claimed_by_user_id": actor.id, "claimed_at": now, "updated_at": now}, synchronize_session=False)
    if claimed != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Esta oportunidade acabou de ser aceita por outro profissional.")
    db.refresh(opportunity)
    lead = db.query(Lead).filter(Lead.id == opportunity.lead_id, Lead.organization_id == actor.organization_id).first() if opportunity.lead_id else None
    notification_ids = []
    if not lead:
        payload = LeadCreate(
            nome=f"Oportunidade {opportunity.public_id}", contato=None, email=None,
            empresa=None, endereco=None, cidade=opportunity.city, estado=opportunity.state,
            pais=opportunity.country or "MX", nicho=opportunity.segment or opportunity.service_type,
            tipo_servico="OUTRO", urgencia=opportunity.urgency, observacoes=opportunity.description_public,
            origen="OTRO",
        )
        lead, notification_ids = create_lead_record(db, payload, actor)
        opportunity.lead_id = lead.id
    else:
        lead.assigned_to_user_id = actor.id
        lead.updated_at = now
    order = db.query(ServiceOrder).filter(ServiceOrder.id == opportunity.service_order_id).first() if opportunity.service_order_id else None
    if not order:
        order = ensure_service_order(db, lead, actor=actor)
        opportunity.service_order_id = order.id
    order.responsible_user_id = actor.id
    order.updated_at = now
    db.add(LeadEvent(organization_id=actor.organization_id, lead_id=lead.id, actor_id=actor.id,
                     actor_name=actor.full_name or actor.username, event_type="MARKETPLACE_OPPORTUNITY_CLAIMED",
                     message=f"Oportunidade {opportunity.public_id} aceita pelo técnico autenticado"))
    db.commit()
    db.refresh(opportunity)
    return {"opportunity": private_opportunity(db, opportunity, actor), "notification_ids": notification_ids}


def seed_demo_opportunities(db: Session, actor: User, count: int = 10) -> int:
    import os
    if os.getenv("ENVIRONMENT", "").lower() == "production" and os.getenv("MARKETPLACE_DEMO_SEED") != "1":
        raise HTTPException(status_code=403, detail="Seed de demonstração desabilitado em produção")
    samples = [("AR-CONDICIONADO", "HOTEL", "Cancún", "ALTA"), ("ELETRICA", "RESIDENCIAL", "Cancún", "NORMAL"),
               ("HIDRAULICA", "HOTEL", "Puerto Morelos", "EMERGENCIA"), ("MANUTENCAO", "COMERCIAL", "Playa del Carmen", "MEDIA")]
    created = 0
    for index in range(count):
        service, segment, city, urgency = samples[index % len(samples)]
        db.add(ServiceOpportunity(public_id=f"DEMO-{uuid4().hex[:10].upper()}", organization_id=actor.organization_id,
                                  service_type=service, segment=segment, city=city, state="Quintana Roo", country="México",
                                  approx_latitude=21.16 + index * 0.01, approx_longitude=-86.85 - index * 0.01,
                                  urgency=urgency, estimated_value_min=1800, estimated_value_max=2500,
                                  description_public="Atendimento de demonstração para testes do radar."))
        created += 1
    db.commit()
    return created
