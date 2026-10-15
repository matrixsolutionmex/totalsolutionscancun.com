import os
import re


_GA4_RE = re.compile(r"^G-[A-Z0-9]+$")
_GOOGLE_ADS_ID_RE = re.compile(r"^AW-[0-9]+$")
_GOOGLE_ADS_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GTM_RE = re.compile(r"^GTM-[A-Z0-9]+$")


def _public_env(name: str, pattern: re.Pattern[str], *, uppercase: bool = True) -> str | None:
    value = os.getenv(name, "").strip()
    if uppercase:
        value = value.upper()
    return value if value and pattern.fullmatch(value) else None


def public_marketing_config() -> dict[str, str | None]:
    """Expose only validated, non-secret measurement identifiers at runtime."""
    return {
        "ga4_measurement_id": _public_env("PUBLIC_GA4_MEASUREMENT_ID", _GA4_RE),
        "google_ads_conversion_id": _public_env("PUBLIC_GOOGLE_ADS_CONVERSION_ID", _GOOGLE_ADS_ID_RE),
        "google_ads_lead_conversion_label": _public_env(
            "PUBLIC_GOOGLE_ADS_LEAD_CONVERSION_LABEL", _GOOGLE_ADS_LABEL_RE, uppercase=False
        ),
        "gtm_container_id": _public_env("PUBLIC_GTM_CONTAINER_ID", _GTM_RE),
    }
