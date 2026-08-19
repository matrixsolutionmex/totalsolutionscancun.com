"""Authenticated, non-persistent image analysis for Pablo."""

import base64
import json
import logging
import re

from app.services.pablo_ai_providers import configured_provider_names, generate_multimodal_from_provider, provider_config

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


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
