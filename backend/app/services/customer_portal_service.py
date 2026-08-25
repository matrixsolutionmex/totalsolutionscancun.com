import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.storage import UPLOADS_DIR
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
from app.models.organization import Organization
from app.models.service_property import ServiceProperty
from app.models.organization_marketplace_link import OrganizationMarketplaceLink
from app.models.service_request import ServiceRequest, ServiceRequestMedia
from app.models.user import User
from app.services.import_service import clean_text, normalize_email, normalize_phone
from app.services.lead_entry_service import duplicate_lead, ensure_property_id
from app.services.service_order_service import ensure_service_order
from app.services.location_service import normalize_service_location
from app.services.route_intelligence_service import calculate_route
from app.services.tracking_health_service import tracking_health
from app.services.tracking_state_service import is_tracking_session_active, tracking_session_state
from app.services.pricing_engine_service import calculate_preliminary_pricing, pricing_snapshot
from app.services.localization_service import normalize_language


ALLOWED_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "video/mp4",
    "video/quicktime",
    "audio/mpeg",
    "audio/mp4",
    "audio/wav",
    "audio/webm",
    "application/pdf",
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _default_organization_id(db: Session) -> int | None:
    organization = db.query(Organization).filter(Organization.status == "ACTIVE").order_by(Organization.id).first()
    if organization:
        return organization.id
    organization = db.query(Organization).order_by(Organization.id).first()
    return organization.id if organization else None


def _safe_filename(filename: str | None) -> str:
    base = Path(filename or "archivo").name
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
    return base or "archivo"


def _public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8010").rstrip("/")


def public_tracking_url(tracking_token: str) -> str:
    return f"{_public_base_url()}/seguimiento/{tracking_token}"


def _actor_name(actor: User | None) -> str:
    if not actor:
        return "Portal del cliente"
    return actor.full_name or actor.username or "Usuario Total Solutions"


def _validate_payload(payload: dict[str, Any]):
    if not payload.get("consent_privacy"):
        raise HTTPException(status_code=400, detail="Debe aceptar la politica de privacidad")
    if not clean_text(payload.get("requester_name")):
        raise HTTPException(status_code=400, detail="Nombre obligatorio")
    if not clean_text(payload.get("requester_phone")) and not normalize_email(payload.get("requester_email")):
        raise HTTPException(status_code=400, detail="Informe telefono o correo")
    if not clean_text(payload.get("address_line1")):
        raise HTTPException(status_code=400, detail="Direccion obligatoria")
    if not clean_text(payload.get("property_type")):
        raise HTTPException(status_code=400, detail="Tipo de inmueble obligatorio")
    if not clean_text(payload.get("service_category")):
        raise HTTPException(status_code=400, detail="Tipo de servicio obligatorio")


def _read_upload(upload) -> bytes:
    upload.file.seek(0)
    content = upload.file.read()
    upload.file.seek(0)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Archivo demasiado grande")
    if upload.content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    if not content:
        raise HTTPException(status_code=400, detail="Archivo vacio")
    return content


def _lead_document_type_for_portal_media(content_type: str | None) -> str:
    mime = (content_type or "").lower()
    if mime.startswith("image/"):
        return "ANTES_SERVICIO"
    if mime.startswith("video/"):
        return "VIDEO"
    return "OTROS"


def _persist_media(db: Session, request: ServiceRequest, uploads: list[Any] | None):
    uploads = [upload for upload in (uploads or []) if getattr(upload, "filename", None)]
    if not uploads:
        return []

    target_dir = Path(UPLOADS_DIR) / "service_requests" / str(request.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for upload in uploads:
        content = _read_upload(upload)
        original = _safe_filename(upload.filename)
        stored_name = f"{secrets.token_hex(12)}-{original}"
        storage_path = target_dir / stored_name
        storage_path.write_bytes(content)
        relative_path = str(storage_path.relative_to(Path(UPLOADS_DIR)))
        record = ServiceRequestMedia(
            organization_id=request.organization_id,
            service_request_id=request.id,
            category="EVIDENCIA_INICIAL",
            original_filename=original,
            storage_path=relative_path,
            content_type=upload.content_type,
            size_bytes=len(content),
        )
        db.add(record)
        if request.lead_id:
            db.add(
                LeadDocument(
                    organization_id=request.organization_id,
                    lead_id=request.lead_id,
                    uploaded_by_user_id=None,
                    document_type=_lead_document_type_for_portal_media(upload.content_type),
                    file_name=original,
                    file_path=f"/uploads/{relative_path}",
                    file_mime=upload.content_type,
                    file_size=len(content),
                )
            )
        records.append(record)
    return records


def _validate_uploads_before_persisting(uploads: list[Any]):
    for upload in uploads:
        _read_upload(upload)


def create_customer_request_and_order(
    db: Session,
    payload: dict[str, Any],
    *,
    files: list[Any] | None = None,
    actor: User | None = None,
    organization_id: int | None = None,
    marketplace_link: OrganizationMarketplaceLink | None = None,
) -> ServiceRequest:
    organization_id = organization_id or (actor.organization_id if actor and actor.organization_id else _default_organization_id(db))
    request_source = "MARKETPLACE_LINK" if marketplace_link else "CLIENT_PORTAL"
    location = normalize_service_location(
        payload.get("location_lat") or payload.get("latitude"),
        payload.get("location_lng") or payload.get("longitude"),
        payload.get("location_accuracy_m"),
        payload.get("location_source"),
        confirmed=bool(payload.get("location_confirmed")),
    )
    if location["location_lat"] is not None and not payload.get("location_confirmed"):
        raise HTTPException(status_code=400, detail="Confirme la ubicacion exacta antes de enviar")
    idempotency_key = clean_text(payload.get("idempotency_key"))
    if idempotency_key and organization_id:
        existing = (
            db.query(ServiceRequest)
            .filter(
                ServiceRequest.organization_id == organization_id,
                ServiceRequest.source == request_source,
                ServiceRequest.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing:
            return existing

    _validate_payload(payload)
    files = [upload for upload in (files or []) if getattr(upload, "filename", None)]
    if not clean_text(payload.get("problem_description")) and not files:
        raise HTTPException(status_code=400, detail="Describa el problema o adjunte una evidencia")
    _validate_uploads_before_persisting(files)

    requester_email = normalize_email(payload.get("requester_email"))
    requester_phone = normalize_phone(payload.get("requester_phone"))
    existing_lead = duplicate_lead(
        db,
        email=requester_email,
        contato=requester_phone,
        whatsapp=requester_phone,
        organization_id=organization_id,
    )
    if existing_lead:
        lead = existing_lead
        lead.updated_at = datetime.utcnow()
    else:
        lead = Lead(
            organization_id=organization_id,
            nome=clean_text(payload.get("requester_name")),
            contato=requester_phone,
            whatsapp=requester_phone,
            email=requester_email,
            endereco=clean_text(payload.get("address_line1")),
            colonia=clean_text(payload.get("district")),
            cidade=clean_text(payload.get("locality")),
            estado=clean_text(payload.get("administrative_area")),
            codigo_postal=clean_text(payload.get("postal_code")),
            google_maps_url=clean_text(payload.get("google_maps_url")),
            latitude=str(location["location_lat"]) if location["location_lat"] is not None else None,
            longitude=str(location["location_lng"]) if location["location_lng"] is not None else None,
            **location,
            tipo_imovel=clean_text(payload.get("property_type")),
            tipo_servico=clean_text(payload.get("service_category")),
            nicho=clean_text(payload.get("service_category")),
            descripcion_problema=clean_text(payload.get("problem_description")),
            urgencia=clean_text(payload.get("urgency")) or "NORMAL",
            origen="PORTAL_CLIENTE",
            pipeline="NOVO LEAD",
            score=0,
            received_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(lead)
        db.flush()
        ensure_property_id(lead)

    if location["location_lat"] is not None:
        lead.latitude = str(location["location_lat"])
        lead.longitude = str(location["location_lng"])
        for field, value in location.items():
            setattr(lead, field, value)

    property_record = ServiceProperty(
        organization_id=organization_id,
        lead_id=lead.id,
        profile_type=clean_text(payload.get("property_type")) or "OUTRO",
        address_line1=clean_text(payload.get("address_line1")),
        address_line2=clean_text(payload.get("address_line2")),
        district=clean_text(payload.get("district")),
        locality=clean_text(payload.get("locality")),
        administrative_area=clean_text(payload.get("administrative_area")),
        country_code=clean_text(payload.get("country_code")) or "MX",
        postal_code=clean_text(payload.get("postal_code")),
        google_maps_url=clean_text(payload.get("google_maps_url")),
        latitude=clean_text(payload.get("latitude")),
        longitude=clean_text(payload.get("longitude")),
        **location,
        access_instructions=clean_text(payload.get("access_instructions")),
    )
    db.add(property_record)
    db.flush()
    property_record.property_code = f"TS-PROP-{property_record.id:06d}"

    try:
        pricing = calculate_preliminary_pricing(
            db, payload.get("service_category"), segment=payload.get("property_type"),
            location={**payload, "distance_km": payload.get("pricing_distance_km"),
                      "duration_minutes": payload.get("pricing_duration_minutes")},
            urgency=payload.get("urgency"), customer_budget_min=payload.get("customer_budget_min"),
            customer_budget_max=payload.get("customer_budget_max"), organization_id=organization_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    service_request = ServiceRequest(
        organization_id=organization_id,
        marketplace_link_id=marketplace_link.id if marketplace_link else None,
        lead_id=lead.id,
        property_id=property_record.id,
        tracking_token=secrets.token_urlsafe(24),
        idempotency_key=idempotency_key,
        source=request_source,
        status="SALES_QUEUE",
        service_category=clean_text(payload.get("service_category")),
        problem_description=clean_text(payload.get("problem_description")),
        urgency=clean_text(payload.get("urgency")) or "NORMAL",
        preferred_visit_at=payload.get("preferred_visit_at"),
        access_instructions=clean_text(payload.get("access_instructions")),
        requester_name=clean_text(payload.get("requester_name")),
        requester_phone=requester_phone,
        requester_email=requester_email,
        public_language=normalize_language(payload.get("public_language")),
        consent_privacy=bool(payload.get("consent_privacy")),
        consent_images=bool(payload.get("consent_images")),
        **location,
    )
    pricing_fields = {
        "pricing_service_type": pricing["service_type"], "pricing_segment": pricing["segment"],
        "pricing_zone": pricing["pricing_zone"], "visit_base_price": pricing["visit_base_price"],
        "travel_surcharge": pricing["travel_surcharge"], "urgency_level": pricing["urgency_level"],
        "urgency_multiplier": pricing["urgency_multiplier"], "visit_calculated_price": pricing["visit_calculated_price"],
        "market_reference_min": pricing["market_reference_min"], "market_reference_max": pricing["market_reference_max"],
        "customer_budget_min": pricing["customer_budget_min"], "customer_budget_max": pricing["customer_budget_max"],
        "pricing_currency": pricing["pricing_currency"], "pricing_version": pricing["pricing_version"],
        "visit_credit_policy": pricing["visit_credit_policy"], "pricing_distance_km": pricing["pricing_distance_km"],
        "pricing_duration_minutes": pricing["pricing_duration_minutes"], "pricing_snapshot_json": pricing_snapshot(pricing),
    }
    for field, value in pricing_fields.items():
        setattr(service_request, field, value)
    db.add(service_request)
    db.flush()

    _persist_media(db, service_request, files)
    service_order = ensure_service_order(
        db,
        lead,
        actor=actor,
        service_request=service_request,
        property_record=property_record,
        force_new=True,
    )
    for field, value in pricing_fields.items():
        if hasattr(service_order, field):
            setattr(service_order, field, value)
    db.add(
        LeadEvent(
            organization_id=organization_id,
            lead_id=lead.id,
            actor_id=actor.id if actor else None,
            actor_name=_actor_name(actor),
            event_type="SERVICE_REQUEST_CREATED",
            message=f"Solicitud publica creada y enviada a cola comercial. OS {service_order.order_number}",
        )
    )
    db.flush()
    return service_request


def service_request_public_status(request: ServiceRequest) -> dict[str, Any]:
    order = request.service_order
    operational_status = _public_operational_status(request, order)
    return {
        "tracking_token": request.tracking_token,
        "language": normalize_language(request.public_language),
        "status": operational_status,
        "operational_status": operational_status,
        "service_category": request.service_category,
        "urgency": request.urgency,
        "created_at": request.created_at,
        "order_number": order.order_number if order else None,
        "tracking_url": public_tracking_url(request.tracking_token),
        "pricing": {
            "service_type": request.pricing_service_type, "segment": request.pricing_segment,
            "zone": request.pricing_zone, "currency": request.pricing_currency,
            "visit_base_price": request.visit_base_price, "travel_surcharge": request.travel_surcharge,
            "urgency_multiplier": request.urgency_multiplier, "visit_calculated_price": request.visit_calculated_price,
            "market_reference_min": request.market_reference_min, "market_reference_max": request.market_reference_max,
            "customer_budget_min": request.customer_budget_min, "customer_budget_max": request.customer_budget_max,
            "estimate_available": request.market_reference_min is not None or request.market_reference_max is not None,
            "requires_diagnosis": True, "visit_credit_policy": request.visit_credit_policy,
            "pricing_version": request.pricing_version,
        },
    }


PUBLIC_OPERATIONAL_STATUS_LABELS = {
    "SALES_QUEUE": "Solicitud recibida",
    "ASSIGNED": "Técnico asignado",
    "ACCEPTED": "Técnico asignado",
    "EN_CAMINO": "Técnico en camino",
    "ARRIVED": "El técnico llegó",
    "EM_ATENDIMENTO": "Servicio en ejecución",
    "IN_PROGRESS": "Servicio en ejecución",
    "COMPLETED": "Servicio finalizado",
    "CONCLUIDA": "Servicio finalizado",
    "FINALIZADA": "Servicio finalizado",
    "CANCELLED": "Servicio cancelado",
    "CANCELADA": "Servicio cancelado",
}


def _public_operational_status(request: ServiceRequest, order) -> str:
    internal_status = (getattr(order, "status", None) or request.status or "SALES_QUEUE").strip().upper()
    language = normalize_language(request.public_language)
    session_state = tracking_session_state(order, getattr(order, "tracking", None))
    if session_state == "STOPPED":
        return {"es": "Ruta finalizada", "en": "Route finished", "pt-BR": "Rota finalizada"}[language]
    if session_state == "ORPHANED":
        return {"es": "Ruta no disponible", "en": "Route unavailable", "pt-BR": "Rota indisponível"}[language]
    labels = {
        "es": PUBLIC_OPERATIONAL_STATUS_LABELS,
        "en": {"SALES_QUEUE": "Request received", "ASSIGNED": "Technician assigned", "ACCEPTED": "Technician assigned", "EN_CAMINO": "Technician on the way", "ARRIVED": "Technician arrived", "EM_ATENDIMENTO": "Service in progress", "CONCLUIDA": "Service completed", "CANCELADA": "Cancelled"},
        "pt-BR": {"SALES_QUEUE": "Solicitação recebida", "ASSIGNED": "Técnico atribuído", "ACCEPTED": "Técnico atribuído", "EN_CAMINO": "Técnico a caminho", "ARRIVED": "Técnico chegou", "EM_ATENDIMENTO": "Serviço em execução", "CONCLUIDA": "Serviço concluído", "CANCELADA": "Cancelada"},
    }
    return labels[language].get(internal_status, labels[language]["SALES_QUEUE"])


def service_request_public_tracking(request: ServiceRequest) -> dict[str, Any]:
    """Serialize only current, token-authorized tracking data for the customer portal."""
    order = request.service_order
    tracking = getattr(order, "tracking", None) if order else None
    tracking_active = is_tracking_session_active(order, tracking)
    property_record = request.property_record
    destination_lat = getattr(order, "location_lat", None) if order else None
    destination_lng = getattr(order, "location_lng", None) if order else None
    if destination_lat is None:
        destination_lat = getattr(property_record, "location_lat", None)
    if destination_lng is None:
        destination_lng = getattr(property_record, "location_lng", None)
    technician = getattr(order, "responsible_user", None) if order else None
    if technician is None and tracking:
        technician = getattr(tracking, "technician", None)
    technician_name = (technician.full_name or technician.username) if technician else None
    health = tracking_health(tracking) if tracking_active else tracking_health(None)
    route = {"available": False, "distance_m": None, "duration_s": None, "eta_at": None, "geometry": None}
    if tracking_active and tracking.current_lat is not None and tracking.current_lng is not None and destination_lat is not None and destination_lng is not None:
        route = calculate_route(
            tracking.current_lat,
            tracking.current_lng,
            destination_lat,
            destination_lng,
            cache_key=f"service-order:{order.id}",
        )
    return {
        "tracking_token": request.tracking_token,
        "language": normalize_language(request.public_language),
        "order_number": order.order_number if order else None,
        "service_category": request.service_category,
        "urgency": request.urgency,
        "tracking_active": tracking_active,
        "operational_status": _public_operational_status(request, order),
        "service_status": _public_operational_status(request, order),
        "technician_display_name": technician_name,
        "route_started_at": tracking.started_at if tracking_active else None,
        "last_location_updated_at": tracking.updated_at if tracking_active else None,
        "last_location_at": health["last_location_at"] if tracking_active else None,
        "seconds_since_last_update": health["seconds_since_last_update"] if tracking_active else None,
        "tracking_health": health["tracking_health"],
        "location_health": health["location_health"],
        "heartbeat_health": health["heartbeat_health"],
        "last_heartbeat_at": health["last_heartbeat_at"] if tracking_active else None,
        "technician_lat": tracking.current_lat if tracking_active else None,
        "technician_lng": tracking.current_lng if tracking_active else None,
        "destination_lat": destination_lat,
        "destination_lng": destination_lng,
        "accuracy_m": tracking.accuracy_m if tracking_active else None,
        "route_available": bool(route.get("available")) if tracking_active and health["tracking_health"] != "OFFLINE" else False,
        "route_distance_m": route.get("distance_m") if tracking_active else None,
        "route_duration_s": route.get("duration_s") if tracking_active else None,
        "route_eta_at": route.get("eta_at") if tracking_active and health["tracking_health"] != "OFFLINE" else None,
        "route_geometry": route.get("geometry") if tracking_active else None,
    }


def service_request_sales_summary(request: ServiceRequest) -> dict[str, Any]:
    lead = request.lead
    order = request.service_order
    return {
        "id": request.id,
        "status": request.status,
        "order_number": order.order_number if order else None,
        "client_id": request.lead_id,
        "client_name": lead.nome if lead else request.requester_name,
        "property_type": request.property_record.profile_type if request.property_record else None,
        "address": request.property_record.address_line1 if request.property_record else None,
        "service_category": request.service_category,
        "urgency": request.urgency,
        "media_count": len(request.media or []),
        "created_at": request.created_at,
        "suggested_supervisor_id": order.supervisor_user_id if order else None,
        "deep_link": f"/?section=clients&lead={request.lead_id}&service_request={request.id}",
    }
