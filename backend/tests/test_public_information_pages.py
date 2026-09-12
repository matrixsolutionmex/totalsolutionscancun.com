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
        assert "/assets/public-site.css?v=9728e26" in body
        assert "/assets/public-site.js?v=9728e26" in body


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


def test_public_seo_files_and_schema_are_reachable():
    robots = get_page("/robots.txt")
    sitemap = get_page("/sitemap.xml")
    home = get_page("/").text

    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://totalsolutionscancun.com/sitemap.xml" in robots.text
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "https://totalsolutionscancun.com/como-funciona" in sitemap.text
    assert "/docs" not in sitemap.text
    assert 'application/ld+json' in home


def test_how_page_does_not_advertise_howto_schema():
    body = get_page("/como-funciona").text

    assert "HowTo" not in body
    assert "application/ld+json" in get_page("/").text


def test_blog_foundation_exposes_index_and_supported_post_shells():
    index = get_page("/blog")
    post = get_page("/blog/cuidar-propiedad-cancun-desde-el-extranjero")

    assert index.status_code == 200
    assert 'data-blog-page="index"' in index.text
    assert "/assets/blog-title-fix.js" in index.text
    assert "/assets/blog.css?v=085f-i18n-fix" in index.text
    assert "Blog" in index.text
    assert post.status_code == 200
    assert 'data-blog-page="post"' in post.text
    assert get_page("/blog/artigo-que-nao-existe").status_code == 404


def test_blog_sitemap_contains_foundation_routes():
    sitemap = get_page("/sitemap.xml").text

    for path in (
        "/blog",
        "/blog/quien-cuida-propiedad-despues-de-la-venta",
        "/blog/cuidar-propiedad-cancun-desde-el-extranjero",
        "/blog/mantenimiento-preventivo-airbnb-cancun",
    ):
        assert f"https://totalsolutionscancun.com{path}" in sitemap


def test_brokers_article_uses_its_dedicated_public_hero_image():
    asset = get_page("/assets/blog/images/quien-cuida-propiedad-despues-de-la-venta.jpg")
    blog_js = get_page("/assets/blog-title-fix.js").text

    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/jpeg")
    assert 'hero_image: "/assets/blog/images/quien-cuida-propiedad-despues-de-la-venta.jpg"' in blog_js
    assert "const localizedArticle = index =>" in blog_js
    assert 'meta[property="og:image"]' in blog_js


def test_remote_owner_article_uses_its_dedicated_public_hero_image():
    asset = get_page("/assets/blog/images/cuidar-propiedad-cancun-desde-el-extranjero.jpg")
    blog_js = get_page("/assets/blog-title-fix.js").text

    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/jpeg")
    assert 'hero_image: "/assets/blog/images/cuidar-propiedad-cancun-desde-el-extranjero.jpg"' in blog_js


def test_airbnb_article_uses_its_dedicated_public_hero_image():
    asset = get_page("/assets/blog/images/mantenimiento-preventivo-airbnb-cancun.jpg")
    blog_js = get_page("/assets/blog-title-fix.js").text

    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/jpeg")
    assert 'hero_image: "/assets/blog/images/mantenimiento-preventivo-airbnb-cancun.jpg"' in blog_js
