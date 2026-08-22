"""Central policy for operational notifications.

This module owns event names, recipient selection, sanitized payloads and
deduplication. It deliberately reuses the existing Notification model.
"""

from enum import Enum

from sqlalchemy.orm import Session

from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.services.notification_service import create_notification
from app.services.localization_service import localized_notification


class OperationalEvent(str, Enum):
    SALES_SERVICE_REQUEST_CREATED = "SALES_SERVICE_REQUEST_CREATED"
    MARKETPLACE_SERVICE_CREATED = "MARKETPLACE_SERVICE_CREATED"
    MARKETPLACE_SERVICE_ACCEPTED = "MARKETPLACE_SERVICE_ACCEPTED"
    MARKETPLACE_SERVICE_ASSIGNED = "MARKETPLACE_SERVICE_ASSIGNED"
    SCHEDULE_PENDING = "SCHEDULE_PENDING"
    SCHEDULE_PROPOSED = "SCHEDULE_PROPOSED"
    SCHEDULE_CONFIRMED = "SCHEDULE_CONFIRMED"
    ROUTE_STARTED = "ROUTE_STARTED"
    TECHNICIAN_NEARBY = "TECHNICIAN_NEARBY"
    TECHNICIAN_ARRIVED = "TECHNICIAN_ARRIVED"
    SERVICE_STARTED = "SERVICE_STARTED"
    QUOTE_READY = "QUOTE_READY"
    QUOTE_APPROVED = "QUOTE_APPROVED"
    SERVICE_COMPLETED = "SERVICE_COMPLETED"
    SERVICE_CANCELLED = "SERVICE_CANCELLED"
    TRACKING_STALE = "TRACKING_STALE"
    TRACKING_OFFLINE = "TRACKING_OFFLINE"
    SLA_WARNING = "SLA_WARNING"
    SLA_BREACHED = "SLA_BREACHED"


class OperationalPriority(str, Enum):
    INFO = "INFO"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


EVENT_PRIORITIES = {
    OperationalEvent.SALES_SERVICE_REQUEST_CREATED: OperationalPriority.NORMAL,
    OperationalEvent.MARKETPLACE_SERVICE_CREATED: OperationalPriority.NORMAL,
}


def get_eligible_technicians_for_opportunity(
    db: Session,
    opportunity: ServiceOpportunity,
) -> list[User]:
    """Return active technicians in the opportunity's organization.

    Role/status are the existing backend authorization gates for marketplace
    access. Distance is intentionally not used as an exclusion criterion.
    """
    return (
        db.query(User)
        .filter(
            User.organization_id == opportunity.organization_id,
            User.role == "BROKER",
            User.status == "ACTIVE",
            User.is_active.is_(True),
        )
        .order_by(User.id.asc())
        .all()
    )


def _recipients_for_event(
    db: Session,
    event_type: OperationalEvent,
    opportunity: ServiceOpportunity,
) -> list[User]:
    if event_type == OperationalEvent.SALES_SERVICE_REQUEST_CREATED:
        return (
            db.query(User)
            .filter(
                User.organization_id == opportunity.organization_id,
                User.role == "ROOT",
                User.status == "ACTIVE",
                User.is_active.is_(True),
            )
            .order_by(User.id.asc())
            .all()
        )
    if event_type != OperationalEvent.MARKETPLACE_SERVICE_CREATED:
        return []

    admins = (
        db.query(User)
        .filter(
            User.organization_id == opportunity.organization_id,
            User.role.in_(["ROOT", "GERENTE"]),
            User.status == "ACTIVE",
            User.is_active.is_(True),
        )
        .order_by(User.id.asc())
        .all()
    )
    technicians = get_eligible_technicians_for_opportunity(db, opportunity)
    return admins + technicians


def _safe_value(value):
    if value is None:
        return None
    return str(value)


def _marketplace_metadata(opportunity: ServiceOpportunity, event_type: OperationalEvent = OperationalEvent.MARKETPLACE_SERVICE_CREATED) -> dict:
    """Keep the marketplace broadcast free of customer PII and private IDs."""
    return {
        "event_type": event_type.value,
        "entity_type": "service_opportunity",
        "entity_public_id": opportunity.public_id,
        "organization_id": opportunity.organization_id,
        "service_type": opportunity.service_type,
        "city": opportunity.city,
        "urgency": opportunity.urgency,
        "pricing_zone": opportunity.pricing_zone,
        "customer_budget_min": _safe_value(opportunity.customer_budget_min),
        "customer_budget_max": _safe_value(opportunity.customer_budget_max),
        "market_reference_min": _safe_value(opportunity.market_reference_min),
        "market_reference_max": _safe_value(opportunity.market_reference_max),
        "visit_calculated_price": _safe_value(opportunity.visit_calculated_price),
        "pricing_currency": opportunity.pricing_currency,
        "pricing_version": opportunity.pricing_version,
    }


def emit_operational_notification(
    db: Session,
    *,
    event_type: OperationalEvent | str,
    organization_id: int,
    actor_user_id: int | None = None,
    service_request_id: int | None = None,
    service_order_id: int | None = None,
    opportunity_id: int | None = None,
    metadata: dict | None = None,
) -> list[int]:
    """Emit notifications for one event without committing the transaction."""
    try:
        event = OperationalEvent(event_type)
    except ValueError as exc:
        raise ValueError(f"Unsupported operational event: {event_type}") from exc

    opportunity = None
    if opportunity_id:
        opportunity = (
            db.query(ServiceOpportunity)
            .filter(
                ServiceOpportunity.id == opportunity_id,
                ServiceOpportunity.organization_id == organization_id,
            )
            .first()
        )
    if event == OperationalEvent.MARKETPLACE_SERVICE_CREATED and not opportunity:
        return []

    recipients = _recipients_for_event(db, event, opportunity) if opportunity else []
    priority = EVENT_PRIORITIES.get(event, OperationalPriority.NORMAL).value
    base_metadata = _marketplace_metadata(opportunity, event) if opportunity else {
        "event_type": event.value,
        "organization_id": organization_id,
    }
    if metadata:
        base_metadata.update({key: value for key, value in metadata.items() if key not in {"email", "phone", "address", "tracking_token"}})

    service = opportunity.service_type or "Servicio" if opportunity else "Evento operativo"
    city = opportunity.city or "zona no informada" if opportunity else "Total Solutions"
    order_number = None
    if opportunity.service_order_id:
        order = db.query(ServiceOrder).filter(ServiceOrder.id == opportunity.service_order_id).first()
        order_number = order.order_number if order else None
    title, message = localized_notification(
        event.value,
        "es",
        service=service,
        city=city,
        order=order_number,
    ) if event in {OperationalEvent.MARKETPLACE_SERVICE_CREATED, OperationalEvent.SALES_SERVICE_REQUEST_CREATED} else (event.value.replace("_", " ").title(), f"{service} · {city}")
    public_id = opportunity.public_id if opportunity else None
    request_identifier = (opportunity.service_request_id or service_request_id) if opportunity else service_request_id
    action_url = (
        f"/?section=clients&lead={opportunity.lead_id}&service_request={request_identifier}"
        if event == OperationalEvent.SALES_SERVICE_REQUEST_CREATED and opportunity else
        f"/?section=agenda&opportunity={public_id}" if public_id else "/"
    )
    dedupe_base = f"{event.value}:org:{organization_id}:opportunity:{public_id or opportunity_id or service_order_id or service_request_id}"

    notification_ids: list[int] = []
    for recipient in recipients:
        if event in {OperationalEvent.MARKETPLACE_SERVICE_CREATED, OperationalEvent.SALES_SERVICE_REQUEST_CREATED}:
            title, message = localized_notification(event.value, recipient.idioma, service=service, city=city, order=order_number)
        notification = create_notification(
            db,
            recipient=recipient,
            actor=db.query(User).filter(User.id == actor_user_id).first() if actor_user_id else None,
            type_=event.value.lower(),
            title=title,
            message=message,
            priority=priority,
            action_url=action_url,
            idempotency_key=f"{dedupe_base}:recipient:{recipient.id}",
            metadata={**base_metadata, "deduplication_key": dedupe_base},
            enqueue_email=False,
        )
        if notification:
            notification_ids.append(notification.id)
    return notification_ids
