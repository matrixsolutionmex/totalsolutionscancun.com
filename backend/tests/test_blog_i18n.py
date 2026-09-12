from app.main import app


def test_blog_reuses_global_language_state_and_translates_all_locales():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    blog_js = (root / "frontend" / "blog.js").read_text()

    assert 'const languageKey = "totalsolutions_public_language"' in blog_js
    assert 'const languages = ["es", "en", "pt-BR"]' in blog_js
    assert 'localStorage.setItem(languageKey, lang)' in blog_js
    assert 'const setLanguage = value =>' in blog_js
    assert 'document.addEventListener("click"' in blog_js
    for text in ("Contenido por contexto", "Content by context", "Conteúdo por contexto"):
        assert text in blog_js
    for text in ("Leer artículo", "Read article", "Ler artigo"):
        assert text in blog_js
    for text in ("¿Quién cuida la propiedad", "Who takes care of the property", "Quem cuida do imóvel"):
        assert text in blog_js


def test_blog_i18n_preserves_internal_filter_keys_and_dynamic_seo():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    blog_js = (root / "frontend" / "blog.js").read_text()

    assert 'data-filter="${filter}"' in blog_js
    assert 'meta[index].category !== filter' in blog_js
    assert 'document.documentElement.lang' in blog_js
    assert 'inLanguage: lang' in blog_js
    assert 'setMeta(\'meta[property="og:image"]\'' in blog_js
    assert 'const renderNotFound' in blog_js


def test_blog_routes_and_i18n_asset_cache_bust_remain_reachable():
    from httpx import ASGITransport, AsyncClient
    import asyncio

    async def request(path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path)

    index = asyncio.run(request("/blog"))
    post = asyncio.run(request("/blog/quien-cuida-propiedad-despues-de-la-venta"))
    missing = asyncio.run(request("/blog/artigo-que-nao-existe"))

    assert index.status_code == 200
    assert "/assets/blog-3f5f73f.js" in index.text
    assert "/assets/blog.css?v=085f-i18n-fix" in index.text
    assert post.status_code == 200
    assert missing.status_code == 404
