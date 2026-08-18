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
from app.services.pablo_ai_service import generate_pablo_reply, pablo_ai_enabled


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

    cleaned = (
        text.replace("?", " ")
        .replace("!", " ")
        .replace(",", " ")
        .replace(".", " ")
        .replace(":", " ")
        .replace(";", " ")
    )
    tokens = set(cleaned.split())

    if tokens.intersection({"chamado", "chamados", "ticket", "tickets", "suporte"}):
        return "tickets"

    if tokens.intersection({
        "pendencia",
        "pendência",
        "pendencias",
        "pendências",
        "notificacao",
        "notificação",
        "notificacoes",
        "notificações",
        "alerta",
        "alertas",
    }):
        return "notifications"

    if tokens.intersection({"cliente", "clientes", "lead", "leads"}):
        return "clients"

    if tokens.intersection({"agenda", "agendado", "agendada", "visita", "visitas"}):
        return "agenda"

    if (
        tokens.intersection({"servico", "serviço", "servicos", "serviços", "ordem", "ordens", "os"})
        or "ordem de serviço" in text
        or "ordem de servico" in text
    ):
        return "services"

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

    visit_scheduled_clients = lead_query.filter(
        Lead.pipeline == "TENTATIVA DE CONTATO"
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
            "visit_scheduled_clients": visit_scheduled_clients,
            "scheduled_service_orders": scheduled_orders,
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
            f"Existem {summary['visit_scheduled_clients']} clientes na etapa Visita agendada. "
            f"Desses, {summary['scheduled_service_orders']} possuem ordem de serviço com "
            "agendamento registrado e ainda não concluído."
        )

    if intent == "agenda":
        return (
            f"Há {summary['visit_scheduled_clients']} clientes na etapa Visita agendada "
            f"e {summary['scheduled_service_orders']} ordens de serviço com agendamento "
            "registrado ainda não concluídas."
        )

    return (
        "Estou conectado à sua operação. "
        f"No seu escopo existem {summary['visible_clients']} clientes, "
        f"{summary['open_tickets']} chamados ativos, "
        f"{summary['unread_notifications']} notificações não lidas, "
        f"{summary['visit_scheduled_clients']} clientes em Visita agendada e "
        f"{summary['scheduled_service_orders']} ordens de serviço com agendamento."
    )


@router.post("/chat", response_model=PabloChatResponse)
def pablo_chat(
    payload: PabloChatRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    message = payload.message.strip()
    intent = detect_intent(message)
    context = build_context(db, actor)

    fallback_reply = build_reply(intent, context)
    reply = fallback_reply

    if pablo_ai_enabled():
        ai_reply = generate_pablo_reply(
            message=message,
            actor=context["actor"],
            context=context,
        )
        if ai_reply:
            reply = ai_reply

    return PabloChatResponse(
        reply=reply,
        intent=intent,
        context=context,
    )
