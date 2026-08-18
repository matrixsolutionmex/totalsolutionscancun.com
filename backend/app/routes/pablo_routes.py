from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.models.lead import Lead
from app.models.notification import Notification
from app.models.service_order import ServiceOrder
from app.models.support_ticket import SupportTicket
from app.models.user import User
from app.routes.lead_routes import apply_actor_scope


router = APIRouter(prefix="/pablo", tags=["pablo-ai"])


class PabloChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class PabloChatResponse(BaseModel):
    reply: str
    intent: str
    context: dict


def normalize_message(value: str) -> str:
    return " ".join(value.strip().lower().split())


def detect_intent(message: str) -> str:
    text = normalize_message(message)

    if any(word in text for word in ("chamado", "chamados", "ticket", "tickets", "suporte")):
        return "tickets"

    if any(word in text for word in ("pendencia", "pendência", "notificacao", "notificação", "alerta")):
        return "notifications"

    if any(word in text for word in ("servico", "serviço", "servicos", "serviços", "ordem", "os ")):
        return "services"

    if any(word in text for word in ("cliente", "clientes", "lead", "leads")):
        return "clients"

    if any(word in text for word in ("agenda", "agendado", "agendada", "visita", "visitas")):
        return "agenda"

    return "general"


def visible_leads(db: Session, actor: User):
    return apply_actor_scope(db.query(Lead), db, actor)


def visible_service_orders(db: Session, actor: User):
    lead_ids = visible_leads(db, actor).with_entities(Lead.id)

    return db.query(ServiceOrder).filter(
        ServiceOrder.organization_id == actor.organization_id,
        ServiceOrder.lead_id.in_(lead_ids),
    )


def visible_tickets(db: Session, actor: User):
    query = db.query(SupportTicket).filter(
        SupportTicket.organization_id == actor.organization_id
    )

    if actor.role == "BROKER":
        query = query.filter(SupportTicket.created_by_user_id == actor.id)
    elif actor.role not in {"ROOT", "GERENTE"}:
        query = query.filter(False)

    return query


def build_context(db: Session, actor: User) -> dict:
    lead_query = visible_leads(db, actor)
    order_query = visible_service_orders(db, actor)
    ticket_query = visible_tickets(db, actor)

    unread_notifications = (
        db.query(Notification)
        .filter(
            Notification.organization_id == actor.organization_id,
            Notification.recipient_user_id == actor.id,
            Notification.read_at.is_(None),
        )
        .count()
    )

    open_tickets = ticket_query.filter(
        SupportTicket.status.in_(["ABERTO", "EM_ATENDIMENTO"])
    ).count()

    scheduled_orders = order_query.filter(
        ServiceOrder.scheduled_at.isnot(None),
        ServiceOrder.completed_at.is_(None),
    ).count()

    return {
        "actor": {
            "id": actor.id,
            "name": actor.full_name or actor.username,
            "role": actor.role,
            "organization_id": actor.organization_id,
        },
        "summary": {
            "visible_clients": lead_query.count(),
            "open_tickets": open_tickets,
            "unread_notifications": unread_notifications,
            "scheduled_services": scheduled_orders,
        },
    }


def build_reply(intent: str, context: dict) -> str:
    summary = context["summary"]

    if intent == "clients":
        return (
            f"Encontrei {summary['visible_clients']} clientes dentro do seu "
            "escopo operacional."
        )

    if intent == "tickets":
        return (
            f"Você possui {summary['open_tickets']} chamados abertos ou em atendimento "
            "dentro do seu escopo."
        )

    if intent == "notifications":
        return (
            f"Você possui {summary['unread_notifications']} notificações não lidas."
        )

    if intent == "services":
        return (
            f"Encontrei {summary['scheduled_services']} serviços agendados "
            "ainda não concluídos dentro do seu escopo."
        )

    if intent == "agenda":
        return (
            f"Há {summary['scheduled_services']} serviços com agendamento "
            "pendente de conclusão."
        )

    return (
        "Estou conectado à sua operação. "
        f"No seu escopo existem {summary['visible_clients']} clientes, "
        f"{summary['open_tickets']} chamados ativos, "
        f"{summary['unread_notifications']} notificações não lidas e "
        f"{summary['scheduled_services']} serviços agendados."
    )


@router.post("/chat", response_model=PabloChatResponse)
def pablo_chat(
    payload: PabloChatRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    intent = detect_intent(payload.message)
    context = build_context(db, actor)

    return PabloChatResponse(
        reply=build_reply(intent, context),
        intent=intent,
        context=context,
    )
