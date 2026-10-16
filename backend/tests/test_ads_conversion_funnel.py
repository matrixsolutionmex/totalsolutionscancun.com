import asyncio

import httpx

from app.main import app
from app.services.customer_portal_service import _public_lead_attribution


def get_page(path: str) -> httpx.Response:
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_quote_landing_is_public_and_preserves_operational_form():
    response = get_page("/cotizar?lang=es")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
    assert "¿Necesitas mantenimiento o reparación en Cancún?" in response.text
    assert "Solicitar cotización por WhatsApp" in response.text
    assert 'action="/public/service-requests"' not in response.text
    assert 'id="quote-form"' in response.text
    assert 'id="quote-service"' in response.text
    assert 'id="quote-name"' in response.text
    assert 'id="quote-phone"' in response.text
    assert 'id="quote-photo"' in response.text
    assert "/assets/cotizar.js?v=3" in response.text
    assert get_page("/solicitar-servico?lang=es").status_code == 200


def test_quote_attribution_accepts_google_ads_without_pii_or_untrusted_origin():
    origin, detail = _public_lead_attribution({
        "lead_origin": "google_ads",
        "attribution": "utm_source=google;utm_campaign=cancun;gclid=synthetic-click",
    })

    assert origin == "GOOGLE_ADS"
    assert detail == "utm_source=google;utm_campaign=cancun;gclid=synthetic-click"

    fallback_origin, fallback_detail = _public_lead_attribution({
        "lead_origin": "PORTAL_CLIENTE",
        "attribution": "customer_email=should-not-be-analytics-data",
    })
    assert fallback_origin == "PORTAL_CLIENTE"
    assert fallback_detail is None


def test_sitemap_contains_quote_landing():
    assert "https://totalsolutionscancun.com/cotizar" in get_page("/sitemap.xml").text


def test_runtime_marketing_config_exposes_only_validated_public_ids(monkeypatch):
    monkeypatch.setenv("PUBLIC_GA4_MEASUREMENT_ID", "g-ABC123")
    monkeypatch.setenv("PUBLIC_GOOGLE_ADS_CONVERSION_ID", "AW-123456")
    monkeypatch.setenv("PUBLIC_GOOGLE_ADS_LEAD_CONVERSION_LABEL", "Lead_Label-01")
    monkeypatch.setenv("PUBLIC_GTM_CONTAINER_ID", "gtm-ABC123")

    response = get_page("/public/marketing-config")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
    assert response.json() == {
        "ga4_measurement_id": "G-ABC123",
        "google_ads_conversion_id": "AW-123456",
        "google_ads_lead_conversion_label": "Lead_Label-01",
        "gtm_container_id": "GTM-ABC123",
    }


def test_runtime_marketing_config_fails_closed_for_invalid_ids(monkeypatch):
    monkeypatch.setenv("PUBLIC_GA4_MEASUREMENT_ID", "https://analytics.example/collect")
    monkeypatch.setenv("PUBLIC_GOOGLE_ADS_CONVERSION_ID", "AW-not-numeric")
    monkeypatch.setenv("PUBLIC_GOOGLE_ADS_LEAD_CONVERSION_LABEL", "label with spaces")
    monkeypatch.setenv("PUBLIC_GTM_CONTAINER_ID", "https://tag.example")

    response = get_page("/public/marketing-config")

    assert response.status_code == 200
    assert response.json() == {
        "ga4_measurement_id": None,
        "google_ads_conversion_id": None,
        "google_ads_lead_conversion_label": None,
        "gtm_container_id": None,
    }
