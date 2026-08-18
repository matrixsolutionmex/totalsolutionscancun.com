"""Shared, permission-preserving Lead creation flow."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.lead_event import LeadEvent
from app.models.user import User
from app.services.lead_entry_service import (
    duplicate_lead,
    duplicate_lead_message,
    ensure_property_id,
    lead_mapping_from_manual,
    validate_responsible,
)
from app.services.notification_service import notify_assignment_change, notify_client_created
from app.services.service_order_service import ensure_service_order


def _add_lead_event(db: Session, lead: Lead, actor: User, event_type: str, message: str):
    db.add(LeadEvent(
        organization_id=lead.organization_id or actor.organization_id,
        lead_id=lead.id,
        actor_id=actor.id,
        actor_name=actor.full_name or actor.username,
        event_type=event_type,
        message=message,
    ))


def create_lead_record(db: Session, payload, actor: User) -> tuple[Lead, list[int]]:
    """Create a Lead using the same mapping, scope and notifications as POST /leads/."""
    mapping = lead_mapping_from_manual(payload, actor=actor)
    mapping["organization_id"] = actor.organization_id
    validate_responsible(db, actor, mapping.get("assigned_to_user_id"))

    duplicate = duplicate_lead(
        db,
        email=mapping.get("email"),
        contato=mapping.get("contato"),
        whatsapp=mapping.get("whatsapp"),
        organization_id=actor.organization_id,
    )
    if duplicate:
        raise HTTPException(status_code=409, detail=duplicate_lead_message(duplicate))

    lead = Lead(**mapping)
    db.add(lead)
    db.flush()
    ensure_property_id(lead)
    service_order = ensure_service_order(db, lead, actor=actor)
    _add_lead_event(
        db,
        lead,
        actor,
        "ENTRADA",
        f"OS {service_order.order_number} criada para cliente com origem {lead.origen or 'OTRO'}",
    )
    notification_ids = notify_client_created(db, lead=lead, actor=actor)
    notification_ids = notify_assignment_change(
        db,
        lead=lead,
        actor=actor,
        previous_user_id=None,
        new_user_id=lead.assigned_to_user_id,
    ) + notification_ids
    return lead, notification_ids
