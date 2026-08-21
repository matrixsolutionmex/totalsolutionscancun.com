from pathlib import Path

from app.services.localization_service import normalize_language


FRONTEND = Path(__file__).parents[2] / "frontend" / "index.html"


def test_user_language_is_normalized_to_supported_backend_values():
    assert normalize_language("pt") == "pt-BR"
    assert normalize_language("pt-BR") == "pt-BR"
    assert normalize_language("en-US") == "en"
    assert normalize_language("es-MX") == "es"
    assert normalize_language("xx") == "es"


def test_authenticated_dashboard_has_one_translated_menu_for_all_supported_languages():
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'nav: ["Serviços", "Clientes", "Agenda", "Relatórios", "Suporte", "Documentos", "Equipe", "Planos"]' in html
    assert 'nav: ["Services", "Clients", "Schedule", "Reports", "Support", "Documents", "Team", "Plans"]' in html
    assert 'nav: ["Servicios", "Clientes", "Agenda", "Reportes", "Soporte", "Documentos", "Equipo", "Planos"]' in html
    assert 'document.querySelectorAll(".mobile-nav [data-mobile-view]")' in html


def test_authenticated_language_selector_updates_only_current_user_profile():
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'id="appLanguage"' in html
    assert '`${API_BASE}/users/${currentUser.id}/profile`' in html
    assert 'body: JSON.stringify({ idioma: language === "pt" ? "pt-BR" : language })' in html
    assert 'organization_language' in html


def test_operational_labels_use_shared_translation_catalog_without_changing_status_values():
    html = FRONTEND.read_text(encoding="utf-8")
    for key in ("uiCustomerBudget", "uiTsReference", "uiRouteStart", "uiRouteStop", "uiSharingLocation"):
        assert f't("{key}")' in html
    assert 'orderStatus === "EN_CAMINO"' in html
    assert 'terminalOrderStatuses = ["COMPLETED", "CONCLUIDA", "FINALIZADA", "CANCELLED", "CANCELADA", "PERDIDO"]' in html
