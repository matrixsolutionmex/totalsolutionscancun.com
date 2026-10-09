from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.models.customer_portal import CustomerServiceLink
from app.models.service_order import ServiceOrder
from app.models.service_property import ServiceProperty
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.customer_account_service import customer_portal_config, customer_portal_available, require_customer
from app.services.customer_invitation_service import consume_existing_customer_invitation
from app.services.customer_portal_service import (
    create_customer_request_and_order,
    public_tracking_url,
    service_request_public_status,
    service_request_public_tracking,
)
from app.services.marketplace_service import create_opportunity_from_service_request


router = APIRouter(prefix="/customer-portal", tags=["customer-portal"])


class CustomerClaimVerification(BaseModel):
    token: str


def _customer_links(db: Session, user: User) -> list[CustomerServiceLink]:
    return db.query(CustomerServiceLink).filter(
        CustomerServiceLink.customer_user_id == user.id,
        CustomerServiceLink.organization_id == user.organization_id,
        CustomerServiceLink.active.is_(True),
        CustomerServiceLink.revoked_at.is_(None),
    ).all()


def _order_projection(order: ServiceOrder, request: ServiceRequest | None, property_record: ServiceProperty | None, tracking: dict | None = None) -> dict:
    tracking = tracking or {}
    return {
        "id": order.id,
        "service_request_id": request.id if request else None,
        "order_number": order.order_number,
        "status": order.status,
        "created_at": order.created_at,
        "scheduled_at": order.scheduled_at,
        "service_category": request.service_category if request else None,
        "urgency": request.urgency if request else None,
        "technician": tracking.get("technician_display_name"),
        "tracking_url": public_tracking_url(request.tracking_token) if request else None,
        "payment_status": tracking.get("payment_status"),
        "documents": [
            {"category": media.category, "name": media.original_filename}
            for media in (request.media or [])
        ] if request else [],
        "property": {
            "id": property_record.id if property_record else None,
            "profile_type": property_record.profile_type if property_record else None,
            "locality": property_record.locality if property_record else None,
            "address_line1": property_record.address_line1 if property_record else None,
        },
    }


def _linked_request(db: Session, user: User, service_request_id: int) -> tuple[ServiceRequest, ServiceOrder, ServiceProperty | None]:
    link = db.query(CustomerServiceLink).filter(
        CustomerServiceLink.customer_user_id == user.id,
        CustomerServiceLink.organization_id == user.organization_id,
        CustomerServiceLink.service_request_id == service_request_id,
        CustomerServiceLink.active.is_(True),
        CustomerServiceLink.revoked_at.is_(None),
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Atendimento não encontrado")
    request = db.query(ServiceRequest).filter(
        ServiceRequest.id == link.service_request_id,
        ServiceRequest.organization_id == user.organization_id,
    ).first()
    order = db.query(ServiceOrder).filter(
        ServiceOrder.service_request_id == link.service_request_id,
        ServiceOrder.organization_id == user.organization_id,
    ).first() if request else None
    if not request or not order:
        raise HTTPException(status_code=404, detail="Atendimento não encontrado")
    property_record = db.query(ServiceProperty).filter(
        ServiceProperty.id == request.property_id,
        ServiceProperty.organization_id == user.organization_id,
    ).first() if request.property_id else None
    return request, order, property_record


def _form_value(form, name: str, default=None):
    value = form.get(name, default)
    return value if value not in (None, "") else default


def _form_bool(form, name: str) -> bool:
    return str(_form_value(form, name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _form_datetime(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Fecha preferida invalida") from exc


@router.get("/config")
def get_customer_portal_config(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    config = customer_portal_config()
    enabled = bool(
        user.role == "CLIENTE"
        and user.is_active
        and user.status == "ACTIVE"
        and customer_portal_available(db, user.organization_id)
    )
    return {"customer_portal_enabled": enabled}


@router.get("/me/dashboard")
def customer_dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_customer(user, db)
    links = _customer_links(db, user)
    orders = []
    properties: dict[int, dict] = {}
    for link in links:
        request = db.query(ServiceRequest).filter(
            ServiceRequest.id == link.service_request_id,
            ServiceRequest.organization_id == user.organization_id,
        ).first()
        if not request:
            continue
        order = db.query(ServiceOrder).filter(
            ServiceOrder.service_request_id == request.id,
            ServiceOrder.organization_id == user.organization_id,
        ).first()
        if not order:
            continue
        property_record = db.query(ServiceProperty).filter(
            ServiceProperty.id == request.property_id,
            ServiceProperty.organization_id == user.organization_id,
        ).first() if request.property_id else None
        try:
            tracking = service_request_public_tracking(request, db)
        except Exception:
            tracking = {}
        orders.append(_order_projection(order, request, property_record, tracking))
        if property_record:
            properties[property_record.id] = {
                "id": property_record.id,
                "profile_type": property_record.profile_type,
                "locality": property_record.locality,
            }
    orders.sort(key=lambda item: item.get("created_at") or datetime.min, reverse=True)
    return {"services": orders, "properties": list(properties.values()), "new_request_url": "/solicitar-servico?customer_portal=1"}


@router.get("/me/service-requests/{service_request_id}")
def customer_service_request_detail(service_request_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_customer(user, db)
    request, order, property_record = _linked_request(db, user, service_request_id)
    try:
        tracking = service_request_public_tracking(request, db)
    except Exception:
        tracking = {}
    return _order_projection(order, request, property_record, tracking)


@router.post("/me/service-requests", status_code=201)
async def create_customer_service_request(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a request from the authenticated customer identity, never from URL identity fields."""
    require_customer(user, db)
    form = await request.form()
    files = [value for value in form.getlist("files") if getattr(value, "filename", None)]
    payload = {
        "requester_name": user.full_name or user.username or "Cliente",
        "requester_phone": user.telefone,
        "requester_email": user.email,
        "property_type": _form_value(form, "property_type"),
        "service_category": _form_value(form, "service_category"),
        "problem_description": _form_value(form, "problem_description"),
        "urgency": _form_value(form, "urgency", "NORMAL"),
        "address_line1": _form_value(form, "address_line1"),
        "address_line2": _form_value(form, "address_line2"),
        "district": _form_value(form, "district"),
        "locality": _form_value(form, "locality"),
        "administrative_area": _form_value(form, "administrative_area"),
        "country_code": _form_value(form, "country_code", "MX"),
        "postal_code": _form_value(form, "postal_code"),
        "google_maps_url": _form_value(form, "google_maps_url"),
        "latitude": _form_value(form, "latitude"),
        "longitude": _form_value(form, "longitude"),
        "location_lat": _form_value(form, "location_lat"),
        "location_lng": _form_value(form, "location_lng"),
        "location_accuracy_m": _form_value(form, "location_accuracy_m"),
        "location_source": _form_value(form, "location_source"),
        "location_confirmed": _form_bool(form, "location_confirmed"),
        "preferred_visit_at": _form_datetime(_form_value(form, "preferred_visit_at")),
        "access_instructions": _form_value(form, "access_instructions"),
        "consent_privacy": _form_bool(form, "consent_privacy"),
        "consent_images": _form_bool(form, "consent_images"),
        "idempotency_key": _form_value(form, "idempotency_key"),
        "public_language": _form_value(form, "public_language"),
        "customer_budget_min": _form_value(form, "customer_budget_min"),
        "customer_budget_max": _form_value(form, "customer_budget_max"),
        "pricing_zone": _form_value(form, "pricing_zone"),
    }
    try:
        service_request = create_customer_request_and_order(
            db, payload, files=files, actor=user, organization_id=user.organization_id,
        )
        link = db.query(CustomerServiceLink).filter_by(
            customer_user_id=user.id, service_request_id=service_request.id,
        ).first()
        if not link:
            link = CustomerServiceLink(
                organization_id=user.organization_id,
                customer_user_id=user.id,
                service_request_id=service_request.id,
                verification_method="authenticated_portal",
                verified_at=datetime.utcnow(),
                active=True,
            )
            db.add(link)
        create_opportunity_from_service_request(db, service_request)
        db.commit()
        db.refresh(service_request)
        return service_request_public_status(service_request)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="No fue posible registrar la solicitud") from exc


@router.post("/claims/verify")
def verify_claim(payload: CustomerClaimVerification, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_customer(user, db)
    consume_existing_customer_invitation(db, payload.token, user)
    db.commit()
    return {"status": "VERIFIED"}
