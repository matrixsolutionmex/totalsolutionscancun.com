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
from app.models.service_request import ServiceRequest, ServiceRequestMedia
from app.models.user import User
from app.services.import_service import clean_text, normalize_email, normalize_phone
from app.services.lead_entry_service import duplicate_lead, ensure_property_id
from app.services.service_order_service import ensure_service_order
from app.services.location_service import normalize_service_location


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
) -> ServiceRequest:
    organization_id = actor.organization_id if actor and actor.organization_id else _default_organization_id(db)
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
                ServiceRequest.source == "CLIENT_PORTAL",
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

    service_request = ServiceRequest(
        organization_id=organization_id,
        lead_id=lead.id,
        property_id=property_record.id,
        tracking_token=secrets.token_urlsafe(24),
        idempotency_key=idempotency_key,
        source="CLIENT_PORTAL",
        status="SALES_QUEUE",
        service_category=clean_text(payload.get("service_category")),
        problem_description=clean_text(payload.get("problem_description")),
        urgency=clean_text(payload.get("urgency")) or "NORMAL",
        preferred_visit_at=payload.get("preferred_visit_at"),
        access_instructions=clean_text(payload.get("access_instructions")),
        requester_name=clean_text(payload.get("requester_name")),
        requester_phone=requester_phone,
        requester_email=requester_email,
        public_language=clean_text(payload.get("public_language")) or "es-MX",
        consent_privacy=bool(payload.get("consent_privacy")),
        consent_images=bool(payload.get("consent_images")),
        **location,
    )
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
    return {
        "tracking_token": request.tracking_token,
        "status": request.status,
        "service_category": request.service_category,
        "urgency": request.urgency,
        "created_at": request.created_at,
        "order_number": order.order_number if order else None,
        "tracking_url": f"{_public_base_url()}/seguimiento/{request.tracking_token}",
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
