from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.models.user import User
from app.services.pablo_audio_service import MAX_AUDIO_BYTES, transcribe_audio
from app.services.pablo_context_service import build_context
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


@router.post("/transcribe")
async def pablo_transcribe(
    file: UploadFile = File(...),
    actor: User = Depends(get_current_user),
):
    del actor  # Authentication is enforced by the dependency; audio is not stored.
    content = await file.read(MAX_AUDIO_BYTES + 1)
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio excede o tamanho maximo permitido")
    text = transcribe_audio(
        filename=file.filename or "recording.webm",
        content=content,
        content_type=file.content_type,
    )
    if not text:
        raise HTTPException(status_code=503, detail="Transcricao indisponivel neste momento")
    return {"text": text}
