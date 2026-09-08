from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.public_tracking_state_service import resolve_public_tracking_state


def order(status):
    return SimpleNamespace(status=status)


def tracking(*, active=False, stopped=False, gps=False):
    return SimpleNamespace(
        tracking_active=active,
        stopped_at=datetime.utcnow() if stopped else None,
        current_lat=21.16 if gps else None,
        current_lng=-86.85 if gps else None,
    )


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_label"),
    [
        ("SALES_QUEUE", "REQUEST_RECEIVED", "Solicitud recibida"),
        ("ASSIGNED", "TECHNICIAN_ASSIGNED", "Técnico asignado"),
        ("EN_CAMINO", "TECHNICIAN_EN_ROUTE", "Técnico en camino"),
        ("EM_ATENDIMENTO", "SERVICE_IN_PROGRESS", "Servicio en ejecución"),
        ("COMPLETED", "SERVICE_COMPLETED", "Servicio finalizado"),
        ("CANCELADA", "CANCELLED", "Servicio cancelado"),
    ],
)
def test_public_tracking_state_maps_operational_statuses(status, expected_code, expected_label):
    state = resolve_public_tracking_state(order(status), tracking(), language="es")
    assert state["code"] == expected_code
    assert state["label"] == expected_label


def test_en_camino_never_becomes_route_finished_when_tracking_session_is_stopped():
    state = resolve_public_tracking_state(order("EN_CAMINO"), tracking(stopped=True), language="es")
    assert state["code"] == "TECHNICIAN_EN_ROUTE"
    assert state["route_state"] == "EN_CAMINO"
    assert state["label"] == "Técnico en camino"


def test_en_camino_without_gps_keeps_route_state_but_marks_map_unavailable():
    state = resolve_public_tracking_state(order("EN_CAMINO"), tracking(), language="en")
    assert state["code"] == "TECHNICIAN_EN_ROUTE"
    assert state["map_state"] == "UNAVAILABLE"
    assert state["label"] == "Technician on the way"


def test_active_tracking_is_en_route_even_when_legacy_order_status_is_unknown():
    state = resolve_public_tracking_state(order("LEGACY_STATUS"), tracking(active=True, gps=True), language="pt-BR")
    assert state["code"] == "TECHNICIAN_EN_ROUTE"
    assert state["map_state"] == "ACTIVE"
    assert state["label"] == "Técnico a caminho"


def test_payment_is_complementary_and_does_not_override_operational_state():
    state = resolve_public_tracking_state(
        order("EN_CAMINO"), tracking(active=True, gps=True), payment=object(), language="es"
    )
    assert state["code"] == "TECHNICIAN_EN_ROUTE"
    assert state["payment_complementary"] is True


@pytest.mark.parametrize(
    ("language", "expected"),
    [("es", "Técnico en camino"), ("en", "Technician on the way"), ("pt-BR", "Técnico a caminho")],
)
def test_public_tracking_state_is_localized(language, expected):
    assert resolve_public_tracking_state(order("EN_CAMINO"), language=language)["label"] == expected
