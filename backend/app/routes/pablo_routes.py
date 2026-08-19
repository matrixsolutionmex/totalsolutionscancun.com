import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user, get_db
from app.models.user import User
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
from app.core.storage import UPLOADS_DIR
from app.routes.lead_routes import ensure_lead_visible_to_actor, actor_label
from app.services.pablo_actions_service import (
    cancel_client_draft,
    confirm_client_draft,
    confirm_operational_proposal,
    cancel_operational_proposal,
    correct_client_draft,
    correct_operational_proposal,
    get_operational_proposal,
    get_client_draft,
    is_create_client_request,
    process_operational_message,
    process_client_message,
)
from app.services.pablo_audio_service import MAX_AUDIO_BYTES, transcribe_audio
from app.services.pablo_context_service import build_context
from app.services.pablo_ai_service import generate_pablo_reply, pablo_ai_enabled
from app.services.pablo_vision_service import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    analyze_image,
    create_vision_session,
    discard_vision,
    get_active_vision,
    public_vision,
    valid_image_bytes,
)


router = APIRouter(prefix="/pablo", tags=["pablo-ai"])
logger = logging.getLogger(__name__)


class PabloChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class PabloChatResponse(BaseModel):
    reply: str
    intent: str
    context: dict
    action_proposal: dict | None = None


class PabloActionMessage(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


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


def action_reply(proposal: dict) -> str:
    if proposal.get("action") != "CREATE_CLIENT":
        if proposal.get("status") == "PENDING_INPUT":
            field = (proposal.get("missing_fields") or ["client"])[0]
            prompts = {
                "client": "Qual é o cliente ou número da OS?",
                "pipeline_stage": "Qual é a etapa real de destino?",
                "changes": "Qual campo devo alterar e qual é o novo valor?",
                "service_order_change": "Qual dado da OS devo atualizar?",
                "value": "Qual é o novo valor?",
            }
            return prompts.get(field, "Preciso de mais um dado para preparar a ação.")
        target = proposal.get("target", {})
        if proposal["action"] == "CHANGE_PIPELINE":
            return f"Vou mover {target.get('name')} de {proposal.get('current', {}).get('pipeline')} para {proposal.get('changes', {}).get('pipeline')}. Confirme para executar."
        if proposal["action"] == "ADD_NOTE":
            return f"Vou registrar uma nota para {target.get('name')}. Confirme para executar."
        if proposal["action"] == "ATTACH_EVIDENCE":
            return f"Vou anexar a evidência de {target.get('name')} à OS {target.get('order_number') or ''}. Confirme para executar."
        return f"Preparei a ação {proposal['action']} para {target.get('name')}. Confira e confirme para executar."
    if proposal["status"] == "PENDING_INPUT":
        questions = {
            "name": "Qual é o nome do cliente?",
            "phone_or_email": "Qual telefone ou email devo registrar?",
        }
        field = proposal["missing_fields"][0]
        return questions.get(field, "Envie o próximo dado do cliente.")
    data = proposal["data"]
    return (
        "Preparei o cadastro. Confira os dados e confirme quando estiver tudo correto: "
        f"{data.get('name')}"
        f"({data.get('phone') or data.get('email')})."
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

    proposal = process_client_message(actor, message)
    if proposal is None:
        proposal = process_operational_message(db, actor, message)
    if proposal is None:
        # A pending proposal must never fall through to conversational LLM output.
        proposal = get_operational_proposal(actor) or get_client_draft(actor)
    if proposal is None and is_create_client_request(message):
        logger.warning("Pablo create-client intent produced no draft: actor_id=%s", actor.id)
    if proposal:
        logger.info(
            "Pablo structured action response: action=%s status=%s actor_id=%s",
            proposal.get("action"),
            proposal.get("status"),
            actor.id,
        )
        return PabloChatResponse(
            reply=action_reply(proposal),
            intent=proposal.get("action", "general"),
            context=context,
            action_proposal=proposal,
        )

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
        action_proposal=None,
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


@router.post("/vision/analyze")
async def pablo_vision_analyze(
    file: UploadFile = File(...),
    actor: User = Depends(get_current_user),
):
    content = await file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Imagem excede o tamanho maximo permitido")
    if file.content_type not in ALLOWED_IMAGE_TYPES or not valid_image_bytes(content, file.content_type):
        raise HTTPException(status_code=415, detail="Formato de imagem invalido")
    analysis = analyze_image(content, file.content_type)
    vision = create_vision_session(actor, content, file.content_type, file.filename or "pablo-image", analysis)
    return {"vision": vision, "persisted": False}


@router.get("/vision/current")
def pablo_current_vision(actor: User = Depends(get_current_user)):
    session = get_active_vision(actor)
    return {"vision": session and public_vision(session)}


@router.post("/vision/discard")
def pablo_discard_vision(actor: User = Depends(get_current_user)):
    return {"status": "DISCARDED", "discarded": discard_vision(actor)}


@router.post("/actions/evidence")
async def pablo_attach_evidence(
    lead_id: int = Form(...),
    document_type: str = Form("OTROS"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    ensure_lead_visible_to_actor(db, lead, actor)
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    allowed_types = {"application/pdf", *ALLOWED_IMAGE_TYPES}
    content = await file.read(50 * 1024 * 1024 + 1)
    extension = Path(file.filename or "arquivo").suffix.lower()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Arquivo excede o tamanho maximo permitido")
    if extension not in allowed_extensions or file.content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="Formato de evidência invalido")
    if file.content_type != "application/pdf" and not valid_image_bytes(content, file.content_type):
        raise HTTPException(status_code=415, detail="Conteudo de arquivo invalido")
    folder = UPLOADS_DIR / "lead_documents" / str(lead.id)
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid4().hex}{extension}"
    destination = folder / safe_name
    destination.write_bytes(content)
    document = LeadDocument(
        organization_id=lead.organization_id or actor.organization_id,
        lead_id=lead.id,
        uploaded_by_user_id=actor.id,
        document_type=document_type if document_type else "OTROS",
        file_name=file.filename or "evidencia",
        file_path=f"/uploads/lead_documents/{lead.id}/{safe_name}",
        file_mime=file.content_type,
        file_size=len(content),
    )
    db.add(document)
    db.add(LeadEvent(organization_id=lead.organization_id or actor.organization_id, lead_id=lead.id, actor_id=actor.id, actor_name=actor_label(actor), event_type="DOCUMENTO", message=f"Pablo anexou evidência: {document.document_type}"))
    db.commit()
    db.refresh(document)
    logger.info("Pablo action completed: action=ATTACH_EVIDENCE actor_id=%s", actor.id)
    return {"action": "ATTACH_EVIDENCE", "status": "EXECUTED", "document_id": document.id, "lead_id": lead.id}


@router.post("/actions/client-draft")
def pablo_client_draft(
    payload: PabloActionMessage,
    actor: User = Depends(get_current_user),
):
    proposal = process_client_message(actor, payload.message)
    if not proposal:
        raise HTTPException(status_code=400, detail="A mensagem não inicia um cadastro de cliente")
    return {"reply": action_reply(proposal), "action_proposal": proposal}


@router.get("/actions/client-draft")
def pablo_client_draft_current(actor: User = Depends(get_current_user)):
    return {"action_proposal": get_client_draft(actor)}


@router.post("/actions/client-draft/confirm")
def pablo_client_draft_confirm(
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return confirm_client_draft(db, actor)


@router.post("/actions/client-draft/cancel")
def pablo_client_draft_cancel(actor: User = Depends(get_current_user)):
    return {"status": "CANCELLED", "cancelled": cancel_client_draft(actor)}


@router.get("/actions/current")
def pablo_current_action(actor: User = Depends(get_current_user)):
    return {"action_proposal": get_operational_proposal(actor) or get_client_draft(actor)}


@router.post("/actions/confirm")
def pablo_action_confirm(
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    proposal = get_operational_proposal(actor)
    if proposal:
        return confirm_operational_proposal(db, actor)
    return confirm_client_draft(db, actor)


@router.post("/actions/cancel")
def pablo_action_cancel(actor: User = Depends(get_current_user)):
    if cancel_operational_proposal(actor):
        return {"status": "CANCELLED", "cancelled": True}
    return {"status": "CANCELLED", "cancelled": cancel_client_draft(actor)}


@router.post("/actions/correct")
def pablo_action_correct(actor: User = Depends(get_current_user)):
    proposal = get_operational_proposal(actor)
    if proposal:
        return {"action_proposal": correct_operational_proposal(actor)}
    return {"action_proposal": correct_client_draft(actor)}
