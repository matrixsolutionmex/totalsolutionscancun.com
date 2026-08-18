"""Human-confirmed, actor-scoped actions for Pablo."""

from datetime import datetime, timedelta
from threading import RLock

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lead_event import LeadEvent
from app.models.user import User
from app.schemas.lead_schema import LeadCreate
from app.services.lead_creation_service import create_lead_record
from app.services.notification_service import dispatch_web_push_for_notification_ids


DRAFT_TTL = timedelta(minutes=15)
_drafts: dict[tuple[int | None, int], dict] = {}
_draft_lock = RLock()
_DRAFT_FIELDS = ("name", "phone", "email", "address", "city", "state", "country", "service_type", "service_value")


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


def missing_fields(draft: dict) -> list[str]:
    data = draft["data"]
    missing = []
    if not str(data.get("name") or "").strip():
        missing.append("name")
    if not any(str(data.get(field) or "").strip() for field in ("phone", "email")):
        missing.append("phone_or_email")
    return missing


def _extract_fields(message: str, draft: dict) -> None:
    import re

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
    capture(r"(?:telefone|tel[eé]fono|phone|whatsapp)\s*(?:é|e|es|is|:)?\s*([+\d][\d () .-]{6,})", "phone")
    capture(r"(?:email|e-mail)\s*(?:é|e|es|is|:)?\s*([^,;\s]+@[^,;\s]+)", "email")
    capture(r"(?:endere[cç]o|direccion|address)\s*(?:é|e|es|is|:)?\s*([^;\n]+)", "address")
    capture(r"(?:cidade|ciudad|city)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "city")
    capture(r"(?:estado|state)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "state")
    capture(r"(?:pa[ií]s|country)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "country")
    capture(r"(?:servi[cç]o|servicio|service)\s*(?:é|e|es|is|:)?\s*([^,;\n]+)", "service_type")
    value_match = re.search(r"(?:valor|value|precio)\s*(?:é|e|es|is|:)?\s*[$€MXN\s]*([\d.,]+)", text, re.IGNORECASE)
    if value_match:
        raw = value_match.group(1).replace(".", "").replace(",", ".")
        try:
            data["service_value"] = float(raw)
        except ValueError:
            pass

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
    import re

    text = message.lower()
    return bool(re.search(
        r"(?:cadastrar|cadastre|criar|crear|registrar|registre)\s+(?:um|uma|o|a)?\s*cliente|novo\s+cliente",
        text,
    ))


def process_client_message(actor: User, message: str) -> dict | None:
    key = _key(actor)
    with _draft_lock:
        _cleanup_expired()
        draft = _drafts.get(key)
        if not draft and not is_create_client_request(message):
            return None
        if not draft:
            draft = _new_draft(actor)
            _drafts[key] = draft
        _extract_fields(message, draft)
        draft["status"] = "PENDING_CONFIRMATION" if not missing_fields(draft) else "PENDING_INPUT"
        return _proposal(draft)


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
        endereco=data.get("address"),
        cidade=data.get("city"),
        estado=data.get("state"),
        pais=data.get("country") or "MX",
        nicho=data.get("service_type"),
        tipo_servico="OUTRO",
        valor_negocio=data.get("service_value"),
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
