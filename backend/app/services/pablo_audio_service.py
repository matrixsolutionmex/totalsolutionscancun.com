"""Audio transcription through the configured Pablo provider."""

import json
import logging
import os

import httpx

from app.services.pablo_ai_providers import provider_config


logger = logging.getLogger(__name__)
MAX_AUDIO_BYTES = 10 * 1024 * 1024
DEFAULT_AUDIO_MODEL = "gpt-4o-mini-transcribe"


def _audio_config():
    provider_name = os.getenv("PABLO_AUDIO_PROVIDER", "").strip().lower() or "openai"
    config = provider_config(provider_name)
    if not config:
        return None
    if provider_name != "openai":
        logger.warning("Pablo audio provider is not supported: provider=%s", provider_name)
        return None

    return {
        "provider": config.name,
        "model": os.getenv("PABLO_AUDIO_MODEL", "").strip() or DEFAULT_AUDIO_MODEL,
        "base_url": os.getenv("PABLO_AUDIO_BASE_URL", "").strip().rstrip("/") or config.base_url,
        "api_key": os.getenv("PABLO_AUDIO_API_KEY", "").strip() or config.api_key,
    }


def transcribe_audio(*, filename: str, content: bytes, content_type: str | None, timeout_seconds: float = 45.0) -> str | None:
    if not content or len(content) > MAX_AUDIO_BYTES:
        logger.warning("Pablo audio transcription rejected: error=invalid_size")
        return None
    config = _audio_config()
    if not config or not config["api_key"]:
        logger.warning("Pablo audio transcription unavailable: error=missing_configuration")
        return None

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                f"{config['base_url']}/audio/transcriptions",
                headers={"Authorization": f"Bearer {config['api_key']}"},
                data={"model": config["model"]},
                files={"file": (filename or "recording.webm", content, content_type or "audio/webm")},
            )
        if response.status_code >= 400:
            logger.warning(
                "Pablo audio transcription failed: provider=%s status=%s",
                config["provider"],
                response.status_code,
            )
            return None
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning("Pablo audio transcription failed: error=invalid_json")
            return None
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            logger.warning("Pablo audio transcription failed: error=empty_response")
            return None
        logger.info("Pablo audio transcription succeeded: provider=%s", config["provider"])
        return text.strip()
    except httpx.TimeoutException:
        logger.warning("Pablo audio transcription failed: provider=%s error=timeout", config["provider"])
    except httpx.HTTPError as exc:
        logger.warning("Pablo audio transcription failed: provider=%s error=%s", config["provider"], type(exc).__name__)
    return None
