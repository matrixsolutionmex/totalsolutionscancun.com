"""Provider configuration and HTTP calls for Pablo AI."""

from dataclasses import dataclass
import json
import logging
import os
from typing import Any

import httpx


logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.6-luna"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    base_url: str
    api_key: str


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def configured_provider_names() -> list[str]:
    configured = _env("PABLO_AI_PROVIDERS")
    if configured:
        return [name.strip().lower() for name in configured.split(",") if name.strip()]

    return [_env("PABLO_AI_PROVIDER").lower() or "openai"]


def provider_config(name: str) -> ProviderConfig | None:
    requested_name = name.strip().lower()
    normalized = requested_name
    if requested_name == "selfhosted":
        normalized = "openai_compatible"

    if normalized not in {"openai", "openai_compatible"}:
        logger.warning("Pablo AI provider is not supported: provider=%s", name)
        return None

    prefix = "SELFHOSTED" if requested_name == "selfhosted" else normalized.upper()
    model = _env(f"PABLO_{prefix}_MODEL") or _env("PABLO_AI_MODEL") or DEFAULT_MODEL
    base_url = _env(f"PABLO_{prefix}_BASE_URL") or _env("PABLO_AI_BASE_URL")
    api_key = _env(f"PABLO_{prefix}_API_KEY") or _env("PABLO_AI_API_KEY")

    if normalized == "openai":
        base_url = base_url or "https://api.openai.com/v1"
        api_key = api_key or _env("OPENAI_API_KEY")
    elif not base_url:
        logger.warning("Pablo AI provider is not configured: provider=%s missing base URL", name)
        return None

    if normalized == "openai" and not api_key:
        logger.warning("Pablo AI provider is not configured: provider=%s missing API key", name)
        return None

    return ProviderConfig(
        name=normalized,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
    )


def has_configured_provider() -> bool:
    return any(provider_config(name) for name in configured_provider_names())


def _headers(config: ProviderConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _response_text(data: Any, provider: str) -> str | None:
    if not isinstance(data, dict):
        logger.warning("Pablo AI response invalid: provider=%s error=payload_not_object", provider)
        return None

    if provider == "openai":
        direct = data.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        chunks = []
        for item in data.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict) or content.get("type") != "output_text":
                    continue
                value = content.get("text")
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())
        return "\n".join(chunks).strip() or None

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        logger.warning("Pablo AI response invalid: provider=%s error=missing_choices", provider)
        return None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        return "".join(part for part in parts if isinstance(part, str)).strip() or None
    logger.warning("Pablo AI response invalid: provider=%s error=empty_content", provider)
    return None


def generate_from_provider(
    config: ProviderConfig,
    *,
    message: str,
    instructions: str,
    timeout_seconds: float,
) -> str | None:
    logger.info(
        "Pablo AI provider selected: provider=%s model=%s",
        config.name,
        config.model,
    )
    if config.name == "openai":
        url = f"{config.base_url}/responses"
        payload = {
            "model": config.model,
            "instructions": instructions,
            "input": message,
            "max_output_tokens": 500,
        }
    else:
        url = f"{config.base_url}/chat/completions"
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": message},
            ],
            "max_tokens": 500,
        }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, headers=_headers(config), json=payload)
        if response.status_code >= 400:
            logger.warning(
                "Pablo AI request failed: provider=%s status=%s error=http_error",
                config.name,
                response.status_code,
            )
            return None

        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning("Pablo AI response invalid: provider=%s error=invalid_json", config.name)
            return None
        reply = _response_text(data, config.name)
        if not reply:
            logger.warning("Pablo AI response invalid: provider=%s error=empty_response", config.name)
        else:
            logger.info("Pablo AI request succeeded: provider=%s", config.name)
        return reply
    except httpx.TimeoutException:
        logger.warning("Pablo AI request failed: provider=%s error=timeout", config.name)
    except httpx.HTTPError as exc:
        logger.warning("Pablo AI request failed: provider=%s error=%s", config.name, type(exc).__name__)
    return None


def generate_multimodal_from_provider(
    config: ProviderConfig,
    *,
    image_data_url: str,
    instructions: str,
    timeout_seconds: float,
) -> str | None:
    """Analyze an explicitly selected image without persisting or exposing it."""
    if config.name == "openai":
        url = f"{config.base_url}/responses"
        payload = {
            "model": config.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Analise esta imagem."},
                {"type": "input_image", "image_url": image_data_url},
            ]}],
            "max_output_tokens": 700,
        }
    else:
        url = f"{config.base_url}/chat/completions"
        payload = {
            "model": config.model,
            "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": [
                {"type": "text", "text": "Analise esta imagem."},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]}],
            "max_tokens": 700,
        }
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, headers=_headers(config), json=payload)
        if response.status_code >= 400:
            logger.warning("Pablo vision request failed: provider=%s status=%s", config.name, response.status_code)
            return None
        return _response_text(response.json(), config.name)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Pablo vision request failed: provider=%s error=%s", config.name, type(exc).__name__)
        return None
