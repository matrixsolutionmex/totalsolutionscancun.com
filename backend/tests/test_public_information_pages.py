import asyncio

import httpx

from app.main import app


def get_page(path: str) -> httpx.Response:
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_public_information_pages_are_indexable_and_reachable():
    pages = {
        "/quienes-somos": ("Quiénes somos", "/quienes-somos"),
        "/como-funciona": ("Cómo funciona", "/como-funciona"),
        "/preguntas-frecuentes": ("Preguntas frecuentes", "/preguntas-frecuentes"),
    }

    for path, (title_fragment, canonical) in pages.items():
        response = get_page(path)
        body = response.text

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert title_fragment in body
        assert f"<link rel=\"canonical\" href=\"https://totalsolutionscancun.com{canonical}\">" in body
        assert "/assets/public-site.css" in body
        assert "/assets/public-site.js" in body


def test_public_faq_shell_loads_shared_renderer():
    body = get_page("/preguntas-frecuentes").text

    assert 'data-page="faq"' in body
    assert "Preguntas frecuentes" in body
    assert "Información sencilla" in body


def test_how_page_exposes_the_five_operational_journeys():
    body = get_page("/como-funciona").text

    for anchor in ("#cliente", "#tecnico", "#supervisor", "#organizacion", "#admin"):
        assert anchor in body
    for actor in ("Cliente", "Técnico", "Supervisor", "Organización", "Admin / ROOT"):
        assert actor in body
