"""Human-confirmed, actor-scoped actions for Pablo."""

from datetime import datetime, timedelta
from threading import RLock
import re
import unicodedata
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lead_event import LeadEvent
from app.models.lead import Lead
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.schemas.lead_schema import LeadCreate
from app.services.lead_creation_service import create_lead_record
from app.services.notification_service import dispatch_web_push_for_notification_ids
from app.routes.lead_routes import PIPELINE_STAGES, apply_actor_scope, ensure_lead_visible_to_actor
from app.services.service_order_service import ensure_service_order, sync_service_order_from_lead


DRAFT_TTL = timedelta(minutes=15)
_drafts: dict[tuple[int | None, int], dict] = {}
_action_proposals: dict[tuple[int | None, int], dict] = {}
_draft_lock = RLock()
logger = logging.getLogger(__name__)
_DRAFT_FIELDS = (
    "name", "phone", "email", "company", "address", "city", "state",
    "country", "service_type", "service_value", "urgency", "notes",
)


def _key(actor: User) -> tuple[int | None, int]:
    return actor.organization_id, actor.id


def _cleanup_expired(now: datetime | None = None) -> None:
    current = now or datetime.utcnow()
    expired = [key for key, draft in _drafts.items() if draft["expires_at"] <= current]
    for key in expired:
        _drafts.pop(key, None)


def _new_draft(actor: User) -> dict:
    now = datetime.utcnow()
    return {
        "action": "CREATE_CLIENT",
        "status": "PENDING_INPUT",
        "data": {},
        "created_at": now,
        "expires_at": now + DRAFT_TTL,
        "organization_id": actor.organization_id,
        "user_id": actor.id,
    }


def _proposal(draft: dict) -> dict:
    return {
        "action": draft["action"],
        "status": draft["status"],
        "data": dict(draft["data"]),
        "missing_fields": missing_fields(draft),
        "expires_at": draft["expires_at"].isoformat(),
    }


def _action_key(actor: User) -> tuple[int | None, int]:
    return _key(actor)


def _clean(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _resolve_lead(db: Session, actor: User, reference: str) -> Lead | None:
    reference = _clean(reference).strip(" .,:;!?\"")
    if not reference:
        return None
    query = apply_actor_scope(db.query(Lead), db, actor)
    exact = query.filter(Lead.nome.ilike(reference)).first()
    if exact:
        return exact
    return query.filter(Lead.nome.ilike(f"%{reference}%")).order_by(Lead.id).first()


def _pipeline_stage(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKD", value.lower()).encode("ascii", "ignore").decode()
    aliases = {
        "novo contato": "NOVO LEAD",
        "nuevo contacto": "NOVO LEAD",
        "new contact": "NOVO LEAD",
        "atendimento": "ATENDIMENTO",
        "atencion": "ATENDIMENTO",
        "atención": "ATENDIMENTO",
        "tentativa de contato": "TENTATIVA DE CONTATO",
        "visita agendada": "TENTATIVA DE CONTATO",
        "visita": "VISITA",
        "diagnostico": "VISITA",
        "diagnosis": "VISITA",
        "diagnostic": "VISITA",
        "montagem de pasta": "MONTAGEM DE PASTA",
        "cotizacion enviada": "MONTAGEM DE PASTA",
        "venda ganha": "VENDA GANHA",
        "servicio aprobado": "VENDA GANHA",
        "service won": "VENDA GANHA",
        "perdido": "PERDIDO",
        "cancelado": "PERDIDO",
    }
    return aliases.get(normalized) or (value.upper() if value.upper() in PIPELINE_STAGES else None)


def _detect_action(message: str) -> str | None:
    normalized = unicodedata.normalize("NFKD", message.lower()).encode("ascii", "ignore").decode()
    if is_create_client_request(message):
        return "CREATE_CLIENT"
    if re.search(r"\b(?:mude|mudar|troque|trocar|altere|alterar|atualize|actualiza|actualizar|cambia|cambiar|cambie|change|update|edit|cadastra|cadastre|registre)\b", normalized) and re.search(r"\b(?:telefone|telefono|phone|email|correo|endereco|direccion|address|urgencia|urgency|valor|value|cidade|city|empresa|company)\b", normalized):
        return "UPDATE_CLIENT"
    if re.search(r"\b(?:coloque|colocar|mova|mover|passe|passar|move|put|cambie|cambiar)\b", normalized) and re.search(r"\b(?:diagnostico|diagnosis|diagnostic|atendimento|visita|pipeline|etapa|stage|novo contato|new contact|cotizacao|venda ganha|cancelado)\b", normalized):
        return "CHANGE_PIPELINE"
    if re.search(r"\b(?:registre|registrar|registra|anote|anotar|add note|adicionar nota|nota)\b", normalized):
        return "ADD_NOTE"
    if re.search(r"\b(?:os|ordem de servico|service order|orden de servicio)\b", normalized) and re.search(r"\b(?:atualize|actualiza|update|marque|marca|agende|agenda|defeito|defecto|status|conclu)\b", normalized):
        return "UPDATE_SERVICE_ORDER"
    return None


_UPDATE_FIELD_LABELS = {
    "contato": r"telefone|tel[eé]fono|phone",
    "email": r"email|e-mail|correo",
    "endereco": r"endereco|direcao|direccion|address",
    "cidade": r"cidade|ciudad|city",
    "urgencia": r"urgencia|urgency|prioridad|priority",
    "empresa": r"empresa|company",
}


def _update_field_from_message(message: str) -> tuple[str, str] | None:
    for field, labels in _UPDATE_FIELD_LABELS.items():
        if re.search(rf"\b(?:{labels})\b", message, re.IGNORECASE):
            return field, labels
    return None


def _update_value_from_message(message: str, labels: str) -> str | None:
    target_pattern = rf"(?:{labels})\b.*?\b(?:para|to|a)\s+([^,;\n]+)$"
    direct_pattern = rf"(?:{labels})\s*(?:é|e|es|is|:)\s*([^,;\n]+)"
    target_match = re.search(target_pattern, message, re.IGNORECASE)
    direct_match = re.search(direct_pattern, message, re.IGNORECASE)
    match = target_match or direct_match
    return match.group(1).strip(" .,:;?!") if match else None


def _lead_reference(message: str) -> str:
    possessive = re.search(
        r"\b(?:change|update|edit)\s+([A-ZÀ-Ý][\wÀ-ÿ]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ]+){0,4})['’]s\s+",
        message,
        re.IGNORECASE,
    )
    if possessive:
        return possessive.group(1).strip()
    name = r"([A-ZÀ-Ý][\wÀ-ÿ]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ]+){0,5}?)(?=\s+(?:para|a|to|for|en)\b|[?.!,;:]|$)"
    match = re.search(rf"(?:do|da|de|del|of|for|o|a|el|la)\s+{name}", message)
    if not match:
        match = re.search(rf"\b(?:mover|move|cambiar|change|atualizar|actualizar|update|colocar|put)\s+{name}", message, re.IGNORECASE)
    if match:
        return match.group(1).strip(" .,:;?!")
    return ""


def _build_operational_proposal(db: Session, actor: User, message: str, action: str) -> dict | None:
    reference = _lead_reference(message)
    lead = _resolve_lead(db, actor, reference) if reference else None
    if not lead:
        proposal = {"action": action, "status": "PENDING_INPUT", "missing_fields": ["client"], "target": {"reference": reference}}
        if action == "UPDATE_CLIENT":
            requested_field = _update_field_from_message(message)
            if requested_field:
                proposal["requested_field"] = requested_field[0]
        return proposal

    proposal = {
        "action": action,
        "status": "PENDING_CONFIRMATION",
        "target": {"lead_id": lead.id, "name": lead.nome},
        "current": {},
        "changes": {},
        "created_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + DRAFT_TTL).isoformat(),
    }
    normalized = unicodedata.normalize("NFKD", message.lower()).encode("ascii", "ignore").decode()
    if action == "CHANGE_PIPELINE":
        destination = next((stage for label, stage in {
            "diagnostico": "VISITA", "diagnosis": "VISITA", "diagnostic": "VISITA",
            "atendimento": "ATENDIMENTO", "novo contato": "NOVO LEAD", "new contact": "NOVO LEAD",
            "visita agendada": "TENTATIVA DE CONTATO", "cotizacao enviada": "MONTAGEM DE PASTA",
            "venda ganha": "VENDA GANHA", "cancelado": "PERDIDO",
        }.items() if label in normalized), None)
        if not destination:
            return {**proposal, "status": "PENDING_INPUT", "missing_fields": ["pipeline_stage"]}
        proposal["current"] = {"pipeline": lead.pipeline}
        proposal["changes"] = {"pipeline": destination}
    elif action == "ADD_NOTE":
        marker = re.search(r"(?:que|que|that|nota|note)\s*[:,-]?\s*(.+)$", message, re.IGNORECASE)
        note = (marker.group(1) if marker else message).strip()
        proposal["changes"] = {"note": note}
    elif action == "UPDATE_CLIENT":
        for field, labels in _UPDATE_FIELD_LABELS.items():
            value = _update_value_from_message(message, labels)
            if value:
                proposal["changes"][field] = value

        value_pattern = r"(?:valor|value|precio)\b.*?\b(?:para|to|a)\s*[$€MXN\s]*([\d.,]+)$"
        direct_value_pattern = r"(?:valor|value|precio)\s*(?:é|e|es|is|:)\s*[$€MXN\s]*([\d.,]+)"
        value_match = re.search(value_pattern, message, re.IGNORECASE) or re.search(direct_value_pattern, message, re.IGNORECASE)
        if value_match:
            proposal["changes"]["valor_negocio"] = float(value_match.group(1).replace(".", "").replace(",", "."))
        proposal["current"] = {field: getattr(lead, field, None) for field in proposal["changes"]}
        if not proposal["changes"]:
            return {**proposal, "status": "PENDING_INPUT", "missing_fields": ["changes"]}
    else:
        proposal["current"] = {"status": lead.service_order.status if lead.service_order else None}
        status_match = re.search(r"(?:status|estado)\s*(?:é|e|es|is|:)?\s*([\wÀ-ÿ ]+)", message, re.IGNORECASE)
        if status_match:
            proposal["changes"] = {"status": status_match.group(1).strip(" .,:;?!").upper()}
        else:
            return {**proposal, "status": "PENDING_INPUT", "missing_fields": ["service_order_change"]}
    proposal["missing_fields"] = []
    return proposal


def process_operational_message(db: Session, actor: User, message: str) -> dict | None:
    action = _detect_action(message)
    with _draft_lock:
        pending = _action_proposals.get(_action_key(actor))

    if not action and pending and pending.get("status") == "PENDING_INPUT":
        missing = pending.get("missing_fields") or []
        if "client" in missing:
            lead = _resolve_lead(db, actor, message)
            if not lead:
                return dict(pending)
            pending["target"] = {"lead_id": lead.id, "name": lead.nome}
            pending.pop("missing_fields", None)
            if pending.get("action") == "UPDATE_CLIENT" and pending.get("requested_field"):
                field = pending.pop("requested_field")
                pending["current"] = {field: getattr(lead, field, None)}
                pending["changes"] = {"_pending_field": field}
                pending["missing_fields"] = ["value"]
            else:
                pending["missing_fields"] = []
            return dict(pending)
        if "value" in missing and pending.get("action") == "UPDATE_CLIENT":
            field = pending.get("changes", {}).pop("_pending_field", None)
            if field:
                pending["changes"][field] = message.strip().strip(" .,:;?!")
                pending["missing_fields"] = []
                pending["status"] = "PENDING_CONFIRMATION"
                logger.info("Pablo action proposal created: action=UPDATE_CLIENT")
                return dict(pending)

    if not action or action == "CREATE_CLIENT":
        return None
    logger.info("Pablo action detected: action=%s actor_id=%s", action, actor.id)
    proposal = _build_operational_proposal(db, actor, message, action)
    if proposal:
        with _draft_lock:
            _action_proposals[_action_key(actor)] = proposal
        logger.info("Pablo action proposal created: action=%s", action)
    return proposal


def get_operational_proposal(actor: User) -> dict | None:
    with _draft_lock:
        return dict(_action_proposals.get(_action_key(actor)) or {}) or None


def cancel_operational_proposal(actor: User) -> bool:
    with _draft_lock:
        return _action_proposals.pop(_action_key(actor), None) is not None


def confirm_operational_proposal(db: Session, actor: User) -> dict:
    with _draft_lock:
        proposal = _action_proposals.pop(_action_key(actor), None)
    if not proposal:
        raise HTTPException(status_code=404, detail="Nenhuma ação pendente para confirmar")
    lead_id = proposal.get("target", {}).get("lead_id")
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    ensure_lead_visible_to_actor(db, lead, actor)
    action = proposal["action"]
    changes = proposal.get("changes", {})
    if action == "CHANGE_PIPELINE":
        stage = changes["pipeline"]
        if stage not in PIPELINE_STAGES:
            raise HTTPException(status_code=400, detail="Etapa de pipeline invalida")
        previous = lead.pipeline or "SEM ETAPA"
        lead.pipeline = stage
        lead.pipeline_updated_at = datetime.utcnow()
        lead.updated_at = datetime.utcnow()
        service_order = ensure_service_order(db, lead, actor=actor)
        sync_service_order_from_lead(db, service_order, lead, actor=actor)
        message = f"Moveu de {previous} para {stage}"
    elif action == "UPDATE_CLIENT":
        for field, value in changes.items():
            setattr(lead, field, value)
        lead.updated_at = datetime.utcnow()
        service_order = ensure_service_order(db, lead, actor=actor)
        sync_service_order_from_lead(db, service_order, lead, actor=actor)
        message = f"Atualizou dados do cliente: {', '.join(changes)}"
    elif action == "ADD_NOTE":
        message = changes["note"]
    else:
        service_order = ensure_service_order(db, lead, actor=actor)
        for field, value in changes.items():
            if field not in {"status", "warranty_days", "checklist_status", "signature_status", "warranty_seal_status"}:
                raise HTTPException(status_code=400, detail="Campo de OS não permitido")
            setattr(service_order, field, value)
        service_order.updated_at = datetime.utcnow()
        message = f"Atualizou OS: {', '.join(changes)}"
    event_type = "NOTA" if action == "ADD_NOTE" else f"PABLO_ACTION_{action}"
    db.add(LeadEvent(organization_id=lead.organization_id or actor.organization_id, lead_id=lead.id, actor_id=actor.id, actor_name=actor.full_name or actor.username, event_type=event_type, message=message))
    db.commit()
    logger.info("Pablo action completed: action=%s actor_id=%s", action, actor.id)
    return {"action": action, "status": "EXECUTED", "message": "Ação concluída.", "lead_id": lead.id, "lead_name": lead.nome}


def missing_fields(draft: dict) -> list[str]:
    data = draft["data"]
    missing = []
    if not str(data.get("name") or "").strip():
        missing.append("name")
    if not any(str(data.get(field) or "").strip() for field in ("phone", "email")):
        missing.append("phone_or_email")
    return missing


def _extract_fields(message: str, draft: dict) -> None:
    text = message.strip()
    lower = text.lower()
    data = draft["data"]

    def capture(pattern: str, field: str):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip(" \t:,-")
            if value:
                data[field] = value

    capture(r"(?:nome|nombre|name)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "name")
    capture(r"(?:telefone(?:\s*/\s*whatsapp)?|tel[eé]fono|phone|whatsapp)\s*(?:é|e|es|is|:)?\s*([+\d][\d () .-]{6,})", "phone")
    capture(r"(?:email|e-mail)\s*(?:é|e|es|is|:)?\s*([^,;\s]+@[^,;\s]+)", "email")
    capture(r"(?:empresa|company)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "company")
    capture(r"(?:endere[cç]o|direccion|address)\s*(?:é|e|es|is|:)?\s*([^;\n]+)", "address")
    capture(r"(?:cidade|ciudad|city)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "city")
    capture(r"(?:estado|state)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "state")
    capture(r"(?:pa[ií]s|country)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "country")
    capture(r"(?:tipo\s+de\s+)?(?:servi[cç]o|servicio|service)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "service_type")
    value_match = re.search(r"(?:valor(?:\s+estimado)?|value|precio|estimated\s+value)\s*(?:é|e|es|is|:)?\s*[$€MXN\s]*([\d.,]+)", text, re.IGNORECASE)
    if value_match:
        raw = value_match.group(1).replace(".", "").replace(",", ".")
        try:
            data["service_value"] = float(raw)
        except ValueError:
            pass
    capture(r"(?:urg[eê]ncia|urgency|prioridad|priority)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "urgency")
    capture(r"(?:observa[cç][aã]o|observacion|nota|notes?|observation)\s*(?:é|e|es|is|:)?\s*(.+)", "notes")

    if "@" in text and "email" not in data:
        email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        if email_match:
            data["email"] = email_match.group(0)

    if not data.get("name") and not any(token in lower for token in ("cadastrar", "cadastre", "crear", "criar", "registrar")):
        if not missing_fields(draft) or "phone" not in missing_fields(draft):
            data["name"] = text

    if not data.get("phone") and re.fullmatch(r"[+\d][\d () .-]{6,}", text):
        data["phone"] = text


def is_create_client_request(message: str) -> bool:
    text = unicodedata.normalize("NFKD", message.lower()).encode("ascii", "ignore").decode()
    action = r"(?:crie|criar|cree|crear|create|cadastre|cadastrar|registre|registrar|register|adicionar|add)"
    target = r"(?:cliente|clientes|client|customer|lead|leads)"
    return bool(
        re.search(rf"\b{action}\b.{{0,80}}\b{target}\b", text)
        or re.search(rf"\b(?:novo|nueva?|new)\s+{target}\b", text)
    )


def process_client_message(actor: User, message: str) -> dict | None:
    key = _key(actor)
    with _draft_lock:
        _cleanup_expired()
        draft = _drafts.get(key)
        create_intent = is_create_client_request(message)
        if not draft and not create_intent:
            return None
        if create_intent:
            logger.info("Pablo action detected: action=CREATE_CLIENT actor_id=%s", actor.id)
        if not draft:
            draft = _new_draft(actor)
            _drafts[key] = draft
        _extract_fields(message, draft)
        draft["status"] = "PENDING_CONFIRMATION" if not missing_fields(draft) else "PENDING_INPUT"
        proposal = _proposal(draft)
        if proposal["status"] == "PENDING_CONFIRMATION":
            logger.info("Pablo client draft created: status=PENDING_CONFIRMATION")
        return proposal


def get_client_draft(actor: User) -> dict | None:
    with _draft_lock:
        _cleanup_expired()
        draft = _drafts.get(_key(actor))
        return _proposal(draft) if draft else None


def cancel_client_draft(actor: User) -> bool:
    with _draft_lock:
        return _drafts.pop(_key(actor), None) is not None


def _lead_payload(data: dict) -> LeadCreate:
    return LeadCreate(
        nome=data.get("name"),
        contato=data.get("phone"),
        email=data.get("email"),
        empresa=data.get("company"),
        endereco=data.get("address"),
        cidade=data.get("city"),
        estado=data.get("state"),
        pais=data.get("country") or "MX",
        nicho=data.get("service_type"),
        tipo_servico="OUTRO",
        valor_negocio=data.get("service_value"),
        urgencia=data.get("urgency") or "NORMAL",
        observacoes=data.get("notes"),
        origen="OTRO",
    )


def confirm_client_draft(db: Session, actor: User) -> dict:
    with _draft_lock:
        _cleanup_expired()
        draft = _drafts.pop(_key(actor), None)
        if not draft:
            raise HTTPException(status_code=404, detail="Nenhum cadastro pendente para confirmar")
        if missing_fields(draft):
            _drafts[_key(actor)] = draft
            raise HTTPException(status_code=400, detail="Cadastro incompleto")

        lead, notification_ids = create_lead_record(db, _lead_payload(draft["data"]), actor)
        db.add(LeadEvent(
            organization_id=actor.organization_id,
            lead_id=lead.id,
            actor_id=actor.id,
            actor_name=actor.full_name or actor.username,
            event_type="PABLO_ACTION_CREATE_CLIENT",
            message="CREATE_CLIENT confirmado pelo usuário autenticado",
        ))
        db.commit()
        dispatch_web_push_for_notification_ids(db, notification_ids)
        db.refresh(lead)
        return {
            "action": "CREATE_CLIENT",
            "status": "EXECUTED",
            "message": "Cliente criado com sucesso.",
            "client_id": lead.id,
            "client_name": lead.nome,
            "notification_ids": notification_ids,
        }
