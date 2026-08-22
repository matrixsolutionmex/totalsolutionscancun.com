"""Canonical language resolution for presentation-layer localization."""

import re


SUPPORTED_LANGUAGES = ("es", "en", "pt-BR")
FALLBACK_LANGUAGE = "es"


def normalize_language(value: str | None) -> str:
    normalized = (value or "").strip().replace("_", "-").lower()
    if normalized.startswith("pt"):
        return "pt-BR"
    if normalized.startswith("en"):
        return "en"
    if normalized.startswith("es"):
        return "es"
    return FALLBACK_LANGUAGE


def resolve_language(*, persisted=None, explicit=None, accept_language=None, fallback=FALLBACK_LANGUAGE) -> str:
    for candidate in (persisted, explicit):
        if candidate:
            return normalize_language(candidate)
    for candidate in re.split(r",\s*", accept_language or ""):
        if candidate:
            return normalize_language(candidate.split(";", 1)[0])
    return normalize_language(fallback)


def locale_for_language(language: str | None) -> str:
    return {"es": "es-MX", "en": "en-US", "pt-BR": "pt-BR"}.get(normalize_language(language), "es-MX")


NOTIFICATION_TRANSLATIONS = {
    "MARKETPLACE_SERVICE_CREATED": {
        "es": ("Nuevo servicio disponible", "{service} · {city}"),
        "en": ("New service available", "{service} · {city}"),
        "pt-BR": ("Novo serviço disponível", "{service} · {city}"),
    },
    "SALES_SERVICE_REQUEST_CREATED": {
        "es": ("Nueva solicitud de servicio", "{order} · {service} · {city}"),
        "en": ("New service request", "{order} · {service} · {city}"),
        "pt-BR": ("Nova solicitação de serviço", "{order} · {service} · {city}"),
    },
}


def localized_notification(
    event_type: str,
    language: str | None,
    *,
    service: str,
    city: str,
    order: str | None = None,
) -> tuple[str, str]:
    translations = NOTIFICATION_TRANSLATIONS.get(event_type, {})
    title, message = translations.get(normalize_language(language), translations.get("es", (event_type, "")))
    return title, message.format(service=service, city=city, order=order or "Total Solutions")
