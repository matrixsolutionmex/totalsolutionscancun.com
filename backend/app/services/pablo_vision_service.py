"""Authenticated, non-persistent image analysis for Pablo."""

import base64
import json
import logging
import re
from datetime import datetime, timedelta
from threading import RLock
from uuid import uuid4

from app.models.user import User

from app.services.pablo_ai_providers import configured_provider_names, generate_multimodal_from_provider, provider_config

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
VISION_TTL = timedelta(minutes=15)
_vision_sessions: dict[tuple[int | None, int], dict] = {}
_vision_lock = RLock()


def valid_image_bytes(content: bytes, content_type: str | None) -> bool:
    if content_type not in ALLOWED_IMAGE_TYPES:
        return False
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    return content.startswith(b"RIFF") and content[8:12] == b"WEBP"


def _structured_result(raw: str | None) -> dict:
    if not raw:
        return {"status": "UNAVAILABLE", "fields": {}, "confidence": None, "notes": "Análise visual indisponível."}
    candidate = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return {"status": "ANALYZED", "fields": value.get("fields", {}), "confidence": value.get("confidence"), "notes": value.get("notes")}
    except (json.JSONDecodeError, TypeError):
        pass
    return {"status": "ANALYZED", "fields": {}, "confidence": None, "notes": candidate}


def analyze_image(content: bytes, content_type: str) -> dict:
    image_data_url = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
    instructions = (
        "Você é o módulo visual do Pablo. Retorne somente JSON válido com as chaves "
        "fields (objeto com dados claramente visíveis), confidence (low, medium ou high) "
        "e notes (string). Não invente valores, não complete campos ausentes e não salve nada. "
        "Indique quando a imagem não permitir uma conclusão confiável."
    )
    raw = None
    for name in configured_provider_names():
        config = provider_config(name)
        if not config:
            continue
        raw = generate_multimodal_from_provider(config, image_data_url=image_data_url, instructions=instructions, timeout_seconds=30)
        if raw:
            break
    logger.info("Pablo vision analyzed: status=%s", "ANALYZED" if raw else "UNAVAILABLE")
    return _structured_result(raw)


def _session_key(actor: User) -> tuple[int | None, int]:
    return actor.organization_id, actor.id


def _cleanup_sessions(now: datetime | None = None) -> None:
    current = now or datetime.utcnow()
    expired = [key for key, session in _vision_sessions.items() if session["expires_at"] <= current]
    for key in expired:
        _vision_sessions.pop(key, None)
        logger.info("Pablo vision expired: actor_id=%s", key[1])


def _safe_display(analysis: dict) -> dict:
    fields = analysis.get("fields") if isinstance(analysis.get("fields"), dict) else {}
    sensitive = {"document_number", "passport_number", "id_number", "birth_date", "date_of_birth", "address", "full_address"}
    display_fields = []
    labels = {
        "document_type": "Tipo", "country": "País", "issuing_country": "País emissor",
        "name": "Titular", "full_name": "Titular", "brand": "Marca", "model": "Modelo",
        "serial": "Serial", "serial_number": "Serial", "capacity": "Capacidade",
    }
    for key, value in fields.items():
        if key in sensitive or value in (None, "") or isinstance(value, (dict, list)):
            continue
        if len(display_fields) >= 5:
            break
        display_fields.append({"label": labels.get(key, key.replace("_", " ").title()), "value": str(value)})
    document_type = str(fields.get("document_type") or fields.get("type") or "Imagem")
    is_sensitive = any(key in fields for key in sensitive) or document_type.lower() in {"passport", "passaporte", "identity", "identidade", "id"}
    return {
        "title": "Documento analisado" if is_sensitive else "Imagem analisada",
        "fields": display_fields,
        "sensitive": is_sensitive,
    }


def create_vision_session(actor: User, content: bytes, content_type: str, filename: str, analysis: dict) -> dict:
    now = datetime.utcnow()
    session = {
        "vision_id": uuid4().hex,
        "status": analysis.get("status", "ANALYZED"),
        "media_type": "image",
        "content_type": content_type,
        "filename": filename,
        "content": content,
        "analysis": analysis,
        "created_at": now,
        "expires_at": now + VISION_TTL,
        "organization_id": actor.organization_id,
        "user_id": actor.id,
    }
    with _vision_lock:
        _cleanup_sessions(now)
        _vision_sessions[_session_key(actor)] = session
    logger.info("Pablo vision analyzed: actor_id=%s type=image", actor.id)
    return public_vision(session)


def get_active_vision(actor: User, vision_id: str | None = None) -> dict | None:
    with _vision_lock:
        _cleanup_sessions()
        session = _vision_sessions.get(_session_key(actor))
        if not session or (vision_id and session["vision_id"] != vision_id):
            return None
        return session


def public_vision(session: dict) -> dict:
    analysis = session.get("analysis") or {}
    fields = analysis.get("fields") if isinstance(analysis.get("fields"), dict) else {}
    document_type = str(fields.get("document_type") or fields.get("type") or "image")
    return {
        "status": session.get("status"),
        "vision_id": session.get("vision_id"),
        "type": "document" if document_type.lower() not in {"image", "photo", "foto"} else "image",
        "document_type": document_type,
        "display": _safe_display(analysis),
        "available_actions": [
            "ATTACH_TO_CLIENT", "ATTACH_TO_SERVICE_ORDER", "APPLY_TO_CLIENT_DRAFT", "DISCARD",
        ],
        "expires_at": session["expires_at"].isoformat(),
    }


def discard_vision(actor: User) -> bool:
    with _vision_lock:
        removed = _vision_sessions.pop(_session_key(actor), None) is not None
    if removed:
        logger.info("Pablo vision discarded: actor_id=%s", actor.id)
    return removed


def expire_vision_for_test(actor: User) -> None:
    with _vision_lock:
        session = _vision_sessions.get(_session_key(actor))
        if session:
            session["expires_at"] = datetime.utcnow() - timedelta(seconds=1)
