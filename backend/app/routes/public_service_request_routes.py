from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db
from app.models.service_request import ServiceRequest
from app.services.customer_portal_service import create_customer_request_and_order, service_request_public_status, service_request_public_tracking
from app.services.marketplace_service import create_opportunity_from_service_request
from app.services.reverse_geocode_service import reverse_geocode


router = APIRouter(prefix="/public", tags=["public-service-requests"])


def _parse_datetime(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Fecha preferida invalida") from exc


@router.get("/geocode/reverse")
def public_reverse_geocode(latitude: float, longitude: float):
    """Return only editable address fields for the public location picker."""
    return reverse_geocode(latitude, longitude)


@router.post("/service-requests", status_code=201)
def create_public_service_request(
    requester_name: str = Form(...),
    requester_phone: str | None = Form(default=None),
    requester_email: str | None = Form(default=None),
    property_type: str = Form(...),
    service_category: str = Form(...),
    problem_description: str | None = Form(default=None),
    urgency: str = Form(default="NORMAL"),
    address_line1: str = Form(...),
    address_line2: str | None = Form(default=None),
    district: str | None = Form(default=None),
    locality: str | None = Form(default=None),
    administrative_area: str | None = Form(default=None),
    country_code: str = Form(default="MX"),
    postal_code: str | None = Form(default=None),
    google_maps_url: str | None = Form(default=None),
    latitude: str | None = Form(default=None),
    longitude: str | None = Form(default=None),
    location_lat: str | None = Form(default=None),
    location_lng: str | None = Form(default=None),
    location_accuracy_m: str | None = Form(default=None),
    location_source: str | None = Form(default=None),
    location_confirmed: bool = Form(default=False),
    preferred_visit_at: str | None = Form(default=None),
    access_instructions: str | None = Form(default=None),
    consent_privacy: bool = Form(default=False),
    consent_images: bool = Form(default=False),
    idempotency_key: str | None = Form(default=None),
    public_language: str = Form(default="es-MX"),
    customer_budget_min: str | None = Form(default=None),
    customer_budget_max: str | None = Form(default=None),
    pricing_zone: str | None = Form(default=None),
    files: list[UploadFile] | None = File(default=None),
    db: Session = Depends(get_db),
):
    payload = {
        "requester_name": requester_name,
        "requester_phone": requester_phone,
        "requester_email": requester_email,
        "property_type": property_type,
        "service_category": service_category,
        "problem_description": problem_description,
        "urgency": urgency,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "district": district,
        "locality": locality,
        "administrative_area": administrative_area,
        "country_code": country_code,
        "postal_code": postal_code,
        "google_maps_url": google_maps_url,
        "latitude": latitude,
        "longitude": longitude,
        "location_lat": location_lat,
        "location_lng": location_lng,
        "location_accuracy_m": location_accuracy_m,
        "location_source": location_source,
        "location_confirmed": location_confirmed,
        "preferred_visit_at": _parse_datetime(preferred_visit_at),
        "access_instructions": access_instructions,
        "consent_privacy": consent_privacy,
        "consent_images": consent_images,
        "idempotency_key": idempotency_key,
        "public_language": public_language,
        "customer_budget_min": customer_budget_min,
        "customer_budget_max": customer_budget_max,
        "pricing_zone": pricing_zone,
    }
    try:
        service_request = create_customer_request_and_order(db, payload, files=files)
        create_opportunity_from_service_request(db, service_request)
        db.commit()
        db.refresh(service_request)
        return service_request_public_status(service_request)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - public endpoint returns a safe generic message.
        db.rollback()
        raise HTTPException(status_code=500, detail="No fue posible registrar la solicitud") from exc


@router.get("/service-requests/{tracking_token}")
def public_service_request_status(tracking_token: str, db: Session = Depends(get_db)):
    service_request = (
        db.query(ServiceRequest)
        .filter(ServiceRequest.tracking_token == tracking_token)
        .first()
    )
    if not service_request:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    return service_request_public_status(service_request)


@router.get("/service-requests/{tracking_token}/tracking")
def public_service_request_tracking(tracking_token: str, db: Session = Depends(get_db)):
    service_request = (
        db.query(ServiceRequest)
        .filter(ServiceRequest.tracking_token == tracking_token)
        .first()
    )
    if not service_request:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    return service_request_public_tracking(service_request)
