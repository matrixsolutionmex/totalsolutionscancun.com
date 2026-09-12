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
    assert 'event.target.closest("[data-lang]")' in blog_js
    assert 'setLanguage(button.dataset.lang)' in blog_js
    assert 'const render = () =>' in blog_js
    assert 'updateDocumentLanguage(); render();' in blog_js
    assert 'renderPost(index)' in blog_js
    assert 'renderNotFound()' in blog_js
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


def test_expanded_editorial_merge_preserves_base_article_contract_for_all_locales():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    blog_js = (root / "frontend" / "blog.js").read_text()

    assert "...meta[index], ...c.articles[index], ...article" in blog_js
    for title in (
        "¿Quién cuida la propiedad después de entregar las llaves?",
        "Cómo cuidar una propiedad en Cancún cuando vives en otra ciudad o país",
        "Mantenimiento preventivo para Airbnb y renta vacacional en Cancún",
        "Who takes care of the property after the keys are handed over?",
        "How to take care of a property in Cancún when you live in another city or country",
        "Preventive maintenance for Airbnb and vacation rentals in Cancún",
        "Quem cuida do imóvel depois da entrega das chaves?",
        "Como cuidar de um imóvel em Cancún morando em outra cidade ou país",
        "Manutenção preventiva para Airbnb e aluguel por temporada em Cancún",
    ):
        assert title in blog_js
    assert "minutes: 3" in blog_js
    assert "hero_image:" in blog_js


def test_editorial_batch_b1_contains_six_localized_articles_and_safe_claims():
    from pathlib import Path

    blog_js = (Path(__file__).resolve().parents[2] / "frontend" / "blog.js").read_text()

    for slug in (
        "como-inmobiliaria-mejorar-servicio-posventa",
        "despues-comprar-propiedad-inversion-cancun",
        "senales-aire-acondicionado-necesita-mantenimiento",
    ):
        assert slug in blog_js
    for title in (
        "Cómo una inmobiliaria puede mejorar su servicio posventa",
        "How a real estate agency can improve its after-sales service",
        "Como uma imobiliária pode melhorar seu pós-venda",
        "Qué hacer después de comprar una propiedad de inversión en Cancún",
        "What to do after buying an investment property in Cancún",
        "O que fazer depois de comprar um imóvel de investimento em Cancún",
        "5 señales de que tu aire acondicionado necesita mantenimiento",
        "5 signs your air conditioner needs maintenance",
        "5 sinais de que seu ar-condicionado precisa de manutenção",
    ):
        assert title in blog_js
    assert "24/7" not in blog_js
    assert "24/7" not in blog_js


def test_editorial_batch_b2_contains_four_pending_localized_articles():
    from pathlib import Path

    blog_js = (Path(__file__).resolve().parents[2] / "frontend" / "blog.js").read_text()

    for slug in (
        "pequenas-fugas-agua-propiedad-cancun",
        "mantenimiento-hoteles-organizar-incidencias",
        "documentar-mantenimiento-fotos-evidencias",
        "como-elegir-tecnicos-confiables-mantenimiento-propiedad",
    ):
        assert slug in blog_js
    for title in (
        "Pequeñas fugas de agua: señales que no conviene ignorar en una propiedad",
        "Small water leaks: signs you should not ignore in a property",
        "Pequenos vazamentos de água: sinais que não convém ignorar em um imóvel",
        "Mantenimiento para hoteles: cómo organizar incidencias sin perder el control",
        "Hotel maintenance: how to organize incidents without losing control",
        "Manutenção para hotéis: como organizar ocorrências sem perder o controle",
        "Fotos, evidencias y seguimiento: por qué documentar cada servicio de mantenimiento",
        "Photos, evidence and follow-up: why document every maintenance service",
        "Fotos, evidências e acompanhamento: por que documentar cada serviço de manutenção",
        "Cómo elegir técnicos confiables para el mantenimiento de una propiedad",
        "How to choose reliable technicians for property maintenance",
        "Como escolher técnicos confiáveis para a manutenção de um imóvel",
    ):
        assert title in blog_js
    assert blog_js.count("image_pending: \"PENDING CUSTOM IMAGE\"") == 0
    assert "24/7" not in blog_js


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
    assert "/assets/blog-085f-b2.js" in index.text
    assert "/assets/blog.css?v=085f-i18n-fix" in index.text
    assert post.status_code == 200
    assert missing.status_code == 404
