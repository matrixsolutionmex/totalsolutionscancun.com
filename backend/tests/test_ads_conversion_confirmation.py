import asyncio
from pathlib import Path

import httpx

from app.main import app


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "frontend" / "index.html"
CONFIRMATION_JS = ROOT / "frontend" / "public-confirmation.js"


def get_page(path: str) -> httpx.Response:
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_confirmation_route_serves_all_supported_languages_without_caching():
    for language in ("es", "en", "pt"):
        response = get_page(f"/solicitud-enviada?lang={language}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"].startswith("no-store")
        assert 'const isConfirmation = path === "/solicitud-enviada"' in response.text


def test_confirmation_page_is_fail_closed_and_never_posts_on_reload():
    html = INDEX.read_text()
    confirmation_branch = html.split("if (isConfirmation) {", 2)[-1]
    assert "confirmationState.read(sessionStorage" in confirmation_branch
    assert "window.location.replace(`/solicitar-servico?lang=" in confirmation_branch
    assert "renderResult(state.confirmation)" in confirmation_branch
    assert "fetch(" not in confirmation_branch.split("} else if (isTracking)", 1)[0]


def test_submit_flow_requires_confirmed_response_before_real_navigation():
    html = INDEX.read_text()
    submit_flow = html.split('form.addEventListener("submit"', 1)[1].split("localStorage.removeItem", 1)[0]
    assert "await confirmationState.submit" in submit_flow
    assert "window.location.assign(url)" in submit_flow
    assert "renderResult(data)" not in submit_flow
    assert "sessionStorage" in submit_flow


def test_confirmation_ui_preserves_content_accessibility_and_tracking_link():
    html = INDEX.read_text()
    result = html.split("function renderResult(data)", 1)[1].split("async function renderTracking", 1)[0]
    for translation_key in ("registered", "order", "trackingCode", "status", "visitReference", "track", "newRequestButton"):
        assert f'pt("{translation_key}")' in result
    assert 'id="confirmationTitle" tabindex="-1"' in result
    assert 'document.getElementById("confirmationTitle")?.focus()' in result
    assert 'href="${escapeHtml(trackingUrl)}"' in result
    assert "confirmationState.clear(sessionStorage)" in result
    assert 'document.documentElement.lang = portalLanguage === "pt-BR" ? "pt-BR" : portalLanguage' in html
    assert 'aria-pressed="${portalLanguage === "es"}"' in html


def test_confirmation_code_has_no_financial_or_identity_requests():
    source = CONFIRMATION_JS.read_text().lower()
    for forbidden in ("stripe", "metamap", "ledger", "earnings", "/payments", "google ads", "gtag", "analytics"):
        assert forbidden not in source
