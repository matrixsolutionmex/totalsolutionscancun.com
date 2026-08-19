"""Build the bounded, actor-scoped operational context sent to Pablo."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.notification import Notification
from app.models.service_order import ServiceOrder
from app.models.support_ticket import SupportTicket
from app.models.user import User
from app.routes.lead_routes import apply_actor_scope
from app.services.marketplace_service import list_opportunities
from app.services.pablo_location_service import get_active_location
from app.services.entitlement_service import PLANS, current_plan
from app.services.commercial_upgrade_service import get_active_upgrade_intent, serialize_upgrade_intent


CLIENT_LIMIT = 30
SERVICE_ORDER_LIMIT = 20
TICKET_LIMIT = 10
NOTIFICATION_LIMIT = 10


def _json_value(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _user_names(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    return {user.id: user.full_name or user.username for user in users}


def _visible_tickets(db: Session, actor: User):
    query = db.query(SupportTicket).filter(
        SupportTicket.organization_id == actor.organization_id
    )
    if actor.role == "BROKER":
        return query.filter(SupportTicket.created_by_user_id == actor.id)
    if actor.role in {"ROOT", "GERENTE"}:
        return query
    return query.filter(False)


def _lead_dict(lead: Lead, assigned_name: str | None, service_order_id: int | None) -> dict:
    return {
        "id": lead.id,
        "name": lead.nome,
        "phone": lead.contato,
        "whatsapp": lead.whatsapp,
        "email": lead.email,
        "company": lead.empresa,
        "pipeline": lead.pipeline,
        "service_type": lead.tipo_servico,
        "service_value": _json_value(lead.valor_negocio),
        "urgency": lead.urgencia,
        "score": lead.score,
        "assigned_to": assigned_name,
        "next_contact": _json_value(lead.proximo_contacto),
        "city": lead.cidade,
        "state": lead.estado,
        "country": lead.pais,
        "received_at": _json_value(lead.received_at),
        "service_order_id": service_order_id,
    }


def _order_dict(order: ServiceOrder, names: dict[int, str]) -> dict:
    return {
        "id": order.id,
        "lead_id": order.lead_id,
        "order_number": order.order_number,
        "status": order.status,
        "scheduled_at": _json_value(order.scheduled_at),
        "completed_at": _json_value(order.completed_at),
        "supervisor": names.get(order.supervisor_user_id),
        "technician": names.get(order.responsible_user_id),
        "signature_status": order.signature_status,
        "checklist_status": order.checklist_status,
        "opened_at": _json_value(order.opened_at),
    }


def _ticket_dict(ticket: SupportTicket) -> dict:
    return {
        "id": ticket.id,
        "protocol": ticket.protocol,
        "module": ticket.module,
        "priority": ticket.priority,
        "status": ticket.status,
        "message": ticket.message,
        "created_at": _json_value(ticket.created_at),
        "updated_at": _json_value(ticket.updated_at),
    }


def _notification_dict(notification: Notification) -> dict:
    return {
        "id": notification.id,
        "type": notification.type,
        "title": notification.title,
        "message": notification.message,
        "priority": notification.priority,
        "read_at": _json_value(notification.read_at),
        "created_at": _json_value(notification.created_at),
    }


def build_context(db: Session, actor: User) -> dict:
    visible_leads_query = apply_actor_scope(db.query(Lead), db, actor)
    total_clients = visible_leads_query.count()
    urgency_rank = case(
        (Lead.urgencia.in_(["CRITICA", "CRÍTICA", "ALTA"]), 0),
        (Lead.urgencia == "MEDIA", 1),
        else_=2,
    )
    leads = visible_leads_query.order_by(
        urgency_rank,
        Lead.proximo_contacto.is_(None),
        Lead.proximo_contacto.asc(),
        Lead.updated_at.desc(),
    ).limit(CLIENT_LIMIT).all()
    lead_ids = [lead.id for lead in leads]

    all_orders_query = db.query(ServiceOrder).filter(
        ServiceOrder.organization_id == actor.organization_id,
    )
    if lead_ids:
        all_orders_query = all_orders_query.filter(ServiceOrder.lead_id.in_(
            visible_leads_query.with_entities(Lead.id)
        ))
    else:
        all_orders_query = all_orders_query.filter(False)
    total_orders = all_orders_query.count()
    orders = all_orders_query.order_by(
        ServiceOrder.scheduled_at.is_(None),
        ServiceOrder.scheduled_at.asc(),
        ServiceOrder.updated_at.desc(),
    ).limit(SERVICE_ORDER_LIMIT).all()

    assigned_ids = {lead.assigned_to_user_id for lead in leads if lead.assigned_to_user_id}
    order_user_ids = {
        user_id
        for order in orders
        for user_id in (order.supervisor_user_id, order.responsible_user_id)
        if user_id
    }
    names = _user_names(db, assigned_ids | order_user_ids)
    orders_by_lead = {}
    for order in orders:
        orders_by_lead.setdefault(order.lead_id, []).append(order)

    clients = [
        _lead_dict(lead, names.get(lead.assigned_to_user_id), max(
            (order.id for order in orders_by_lead.get(lead.id, [])),
            default=None,
        ))
        for lead in leads
    ]

    tickets_query = _visible_tickets(db, actor)
    total_tickets = tickets_query.count()
    tickets = tickets_query.order_by(
        SupportTicket.status.asc(),
        SupportTicket.updated_at.desc(),
    ).limit(TICKET_LIMIT).all()

    notifications_query = db.query(Notification).filter(
        Notification.organization_id == actor.organization_id,
        Notification.recipient_user_id == actor.id,
    )
    total_notifications = notifications_query.count()
    notifications = notifications_query.order_by(
        Notification.read_at.is_(None).desc(),
        Notification.created_at.desc(),
    ).limit(NOTIFICATION_LIMIT).all()

    open_tickets = tickets_query.filter(
        SupportTicket.status.in_(["ABERTO", "EM_ATENDIMENTO"])
    ).count()
    unread_notifications = notifications_query.filter(Notification.read_at.is_(None)).count()
    scheduled_orders = all_orders_query.filter(
        ServiceOrder.scheduled_at.isnot(None),
        ServiceOrder.completed_at.is_(None),
    ).count()
    visit_scheduled_clients = visible_leads_query.filter(
        Lead.pipeline == "TENTATIVA DE CONTATO"
    ).count()

    return {
        "actor": {
            "id": actor.id,
            "name": actor.full_name or actor.username,
            "role": actor.role,
            "organization_id": actor.organization_id,
            "language": actor.idioma,
        },
        "summary": {
            "visible_clients": total_clients,
            "open_tickets": open_tickets,
            "unread_notifications": unread_notifications,
            "visit_scheduled_clients": visit_scheduled_clients,
            "scheduled_service_orders": scheduled_orders,
        },
        "clients": clients,
        "service_orders": [_order_dict(order, names) for order in orders],
        "tickets": [_ticket_dict(ticket) for ticket in tickets],
        "notifications": [_notification_dict(notification) for notification in notifications],
        "marketplace": {
            "available": list_opportunities(db, actor, sort="distance")[:10],
            "location_available": bool(get_active_location(actor)),
        },
        "commercial": {
            "plan": current_plan(db, actor),
            "features": sorted(PLANS[current_plan(db, actor)]["features"]),
            "active_intent": serialize_upgrade_intent(get_active_upgrade_intent(db, actor)),
        },
        "limits": {
            "clients": {"total_available": total_clients, "sent": len(clients)},
            "service_orders": {"total_available": total_orders, "sent": len(orders)},
            "tickets": {"total_available": total_tickets, "sent": len(tickets)},
            "notifications": {"total_available": total_notifications, "sent": len(notifications)},
        },
    }
