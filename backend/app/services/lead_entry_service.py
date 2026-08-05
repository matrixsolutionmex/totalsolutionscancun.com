import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.user import User
from app.services.import_service import clean_text, normalize_email, normalize_phone


def phone_candidates(value: str | None) -> set[str]:
    normalized = normalize_phone(value)
    if not normalized:
        return set()

    candidates = {normalized}
    if len(normalized) > 10:
        candidates.add(normalized[-10:])
    return candidates


def property_extra_json(payload) -> str | None:
    if getattr(payload, "property_extra_json", None):
        return clean_text(payload.property_extra_json)
    extra = getattr(payload, "property_extra", None)
    if not extra:
        return None
    return json.dumps(extra, ensure_ascii=False, sort_keys=True)


def ensure_property_id(lead: Lead):
    if not lead.property_id and lead.id:
        lead.property_id = f"TS-{lead.id:06d}"


def broker_ids_for_manager(db: Session, manager_id: int):
    manager = db.query(User).filter(User.id == manager_id).first()
    return [
        broker_id
        for (broker_id,) in (
            db.query(User.id)
            .filter(
                User.role == "BROKER",
                User.manager_id == manager_id,
                User.is_active.is_(True),
                User.organization_id == (manager.organization_id if manager else None),
            )
            .all()
        )
    ]


def validate_responsible(db: Session, actor: User | None, assigned_to_user_id: int | None):
    if assigned_to_user_id is None:
        return None

    responsible = (
        db.query(User)
        .filter(
            User.id == assigned_to_user_id,
            User.role.in_(["GERENTE", "BROKER"]),
            User.is_active.is_(True),
            User.organization_id == actor.organization_id if actor else True,
        )
        .first()
    )
    if not responsible:
        raise HTTPException(status_code=404, detail="Responsavel ativo nao encontrado")

    if actor and actor.role == "BROKER" and responsible.id != actor.id:
        raise HTTPException(status_code=403, detail="Tecnico nao pode redistribuir clientes")

    if actor and actor.role == "GERENTE":
        if responsible.role != "BROKER" or responsible.manager_id != actor.id:
            raise HTTPException(status_code=403, detail="Supervisor pode atribuir apenas para tecnicos da propria equipe")

    return responsible


def duplicate_lead(
    db: Session,
    *,
    email: str | None = None,
    contato: str | None = None,
    whatsapp: str | None = None,
    external_source: str | None = None,
    external_id: str | None = None,
    organization_id: int | None = None,
):
    base_query = db.query(Lead)
    if organization_id:
        base_query = base_query.filter(Lead.organization_id == organization_id)

    if external_source and external_id:
        existing = (
            base_query
            .filter(Lead.external_source == external_source, Lead.external_id == external_id)
            .first()
        )
        if existing:
            return existing

    normalized_email = normalize_email(email)
    normalized_phone_candidates = phone_candidates(whatsapp) | phone_candidates(contato)

    if normalized_email:
        existing = base_query.filter(Lead.email == normalized_email).first()
        if existing:
            return existing

    if normalized_phone_candidates:
        phone_query = db.query(Lead.id, Lead.contato, Lead.whatsapp)
        if organization_id:
            phone_query = phone_query.filter(Lead.organization_id == organization_id)
        for lead in phone_query.all():
            existing_candidates = phone_candidates(lead.contato) | phone_candidates(lead.whatsapp)
            if normalized_phone_candidates & existing_candidates:
                return base_query.filter(Lead.id == lead.id).first()

    return None


def duplicate_lead_message(lead: Lead) -> str:
    service_order = getattr(lead, "service_order", None)
    reference = service_order.order_number if service_order and service_order.order_number else lead.property_id or f"ID {lead.id}"
    contact_parts = []
    if lead.contato:
        contact_parts.append(f"telefone {lead.contato}")
    if lead.whatsapp and lead.whatsapp != lead.contato:
        contact_parts.append(f"WhatsApp {lead.whatsapp}")
    if lead.email:
        contact_parts.append(f"email {lead.email}")
    contact = ", ".join(contact_parts) if contact_parts else "contato ja registrado"
    return f"Cliente duplicado: {contact}. Registro existente: {reference}"


def lead_mapping_from_manual(payload, *, actor: User | None = None):
    now = datetime.utcnow()
    assigned_to_user_id = payload.assigned_to_user_id if payload.assigned_to_user_id is not None else payload.responsable

    if actor and actor.role == "BROKER":
        assigned_to_user_id = actor.id

    return {
        "nome": clean_text(payload.nome or payload.nombre),
        "contato": clean_text(payload.contato or payload.telefono),
        "whatsapp": clean_text(payload.whatsapp),
        "email": normalize_email(payload.email),
        "endereco": clean_text(payload.endereco or payload.direccion),
        "colonia": clean_text(payload.colonia),
        "cidade": clean_text(payload.cidade or payload.ciudad),
        "estado": clean_text(payload.estado),
        "codigo_postal": clean_text(payload.codigo_postal),
        "google_maps_url": clean_text(payload.google_maps_url),
        "nicho": clean_text(payload.nicho or payload.servicio_solicitado or payload.tipo_servico),
        "descripcion_problema": clean_text(payload.descripcion_problema),
        "urgencia": payload.urgencia,
        "origen": payload.origen,
        "origen_detalle": clean_text(payload.origen_detalle),
        "proximo_contacto": payload.proximo_contacto,
        "observacoes": clean_text(payload.observacoes or payload.observaciones),
        "tipo_imovel": payload.tipo_imovel,
        "tipo_servico": payload.tipo_servico,
        "empresa": clean_text(payload.empresa),
        "pessoa_contato": clean_text(payload.pessoa_contato),
        "latitude": clean_text(payload.latitude),
        "longitude": clean_text(payload.longitude),
        "foto_fachada_url": clean_text(payload.foto_fachada_url),
        "property_extra_json": property_extra_json(payload),
        "pais": clean_text(payload.pais or "MX"),
        "assigned_to_user_id": assigned_to_user_id,
        "pipeline": "NOVO LEAD",
        "pipeline_updated_at": now,
        "created_at": now,
        "updated_at": now,
    }


def lead_mapping_from_integration(payload):
    now = datetime.utcnow()
    return {
        "nome": clean_text(payload.nombre),
        "contato": clean_text(payload.telefono),
        "whatsapp": clean_text(payload.whatsapp),
        "email": normalize_email(payload.email),
        "endereco": clean_text(payload.direccion),
        "cidade": clean_text(payload.ciudad),
        "nicho": clean_text(payload.servicio_solicitado or payload.tipo_servico),
        "tipo_imovel": payload.tipo_imovel,
        "tipo_servico": payload.tipo_servico,
        "property_extra_json": property_extra_json(payload),
        "descripcion_problema": clean_text(payload.descripcion_problema),
        "urgencia": "NORMAL",
        "origen": payload.origen,
        "origen_detalle": clean_text(payload.origen_detalle),
        "external_id": clean_text(payload.external_id),
        "external_source": payload.origen,
        "received_at": now,
        "pais": "MX",
        "assigned_to_user_id": None,
        "pipeline": "NOVO LEAD",
        "pipeline_updated_at": now,
        "created_at": now,
        "updated_at": now,
    }
