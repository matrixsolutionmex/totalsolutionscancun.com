from fastapi import APIRouter, Depends
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


def _order_projection(order: ServiceOrder, request: ServiceRequest | None, property_record: ServiceProperty | None) -> dict:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "created_at": order.created_at,
        "scheduled_at": order.scheduled_at,
        "service_category": request.service_category if request else None,
        "property": {
            "id": property_record.id if property_record else None,
            "profile_type": property_record.profile_type if property_record else None,
            "locality": property_record.locality if property_record else None,
        },
    }


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
        orders.append(_order_projection(order, request, property_record))
        if property_record:
            properties[property_record.id] = {
                "id": property_record.id,
                "profile_type": property_record.profile_type,
                "locality": property_record.locality,
            }
    return {"services": orders, "properties": list(properties.values()), "new_request_url": "/solicitar-servico?customer_portal=1"}


@router.post("/claims/verify")
def verify_claim(payload: CustomerClaimVerification, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_customer(user, db)
    consume_existing_customer_invitation(db, payload.token, user)
    db.commit()
    return {"status": "VERIFIED"}
