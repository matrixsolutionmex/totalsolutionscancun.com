from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database.connection import Base
from app.models.lead import Lead
from app.models.lead_event import LeadEvent
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_order_tracking import ServiceOrderTracking
from app.models.service_property import ServiceProperty
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.schemas.lead_schema import LeadResponse
from app.services.service_order_tracking_service import (
    admin_stop_all_tracking,
    admin_stop_tracking,
    diagnose_tracking_for_root,
    get_tracking_for_actor,
    heartbeat_tracking,
    record_tracking_diagnostic,
    list_active_tracking_for_actor,
    route_for_order,
    start_tracking,
    stop_tracking,
    stop_tracking_for_order,
    update_tracking_position,
)
from app.services.tracking_health_service import tracking_health
from app.services.tracking_state_service import is_tracking_session_active, tracking_session_state


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Organization.__table__,
            User.__table__,
            Lead.__table__,
            ServiceProperty.__table__,
            ServiceRequest.__table__,
            ServiceOrder.__table__,
            ServiceOrderTracking.__table__,
            LeadEvent.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_org(db, slug):
    org = Organization(name=slug.title(), slug=slug)
    db.add(org)
    db.commit()
    return org


def make_user(db, username, role, org, manager_id=None):
    user = User(
        username=username,
        email=f"{username}@example.test",
        full_name=username.title(),
        password_hash=hash_password("secret"),
        role=role,
        organization_id=org.id,
        manager_id=manager_id,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    return user


def make_order(db, org, technician, supervisor=None, status="ABERTA"):
    lead = Lead(
        organization_id=org.id,
        nome="Cliente Tracking",
        contato="9980000000",
        pipeline="NOVO LEAD",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lead)
    db.flush()
    order = ServiceOrder(
        organization_id=org.id,
        order_number=f"TS-2026-{lead.id:06d}",
        lead_id=lead.id,
        status=status,
        responsible_user_id=technician.id if technician else None,
        supervisor_user_id=supervisor.id if supervisor else None,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_assigned_technician_starts_with_consent_and_no_automatic_gps(db):
    org = make_org(db, "tracking-start")
    technician = make_user(db, "tecnico", "BROKER", org)
    order = make_order(db, org, technician)

    assert db.query(ServiceOrderTracking).count() == 0
    assert LeadResponse.model_validate(order.lead).service_order.tracking_start_allowed is True
    with pytest.raises(HTTPException) as blocked:
        start_tracking(db, order.id, technician, False)
    assert blocked.value.status_code == 400
    assert db.query(ServiceOrderTracking).count() == 0

    result = start_tracking(db, order.id, technician, True)
    assert result["status"] == "EN_CAMINO"
    assert result["tracking"]["tracking_active"] is True
    assert result["tracking"]["consent_granted_at"] is not None
    assert db.query(ServiceOrderTracking).count() == 1
    assert db.query(LeadEvent).filter(LeadEvent.event_type == "TRACKING_STARTED").count() == 1


def test_tracking_state_is_canonical_and_restart_clears_stopped_at(db):
    org = make_org(db, "tracking-canonical")
    technician = make_user(db, "tecnico-canonical", "BROKER", org)
    order = make_order(db, org, technician)

    start_tracking(db, order.id, technician, True)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    assert tracking_session_state(order, tracking) == "ACTIVE"
    assert is_tracking_session_active(order, tracking) is True

    stop_tracking(db, order.id, technician, "MANUAL")
    db.refresh(order)
    db.refresh(tracking)
    assert tracking_session_state(order, tracking) == "STOPPED"
    assert tracking.tracking_active is False
    assert tracking.stopped_at is not None

    restarted = start_tracking(db, order.id, technician, True)
    db.refresh(tracking)
    assert restarted["status"] == "EN_CAMINO"
    assert tracking.tracking_active is True
    assert tracking.stopped_at is None
    assert tracking_session_state(order, tracking) == "ACTIVE"


def test_tracking_state_marks_active_stopped_and_wrong_status_as_orphaned(db):
    org = make_org(db, "tracking-canonical-orphaned")
    technician = make_user(db, "tecnico-canonical-orphaned", "BROKER", org)
    order = make_order(db, org, technician)
    start_tracking(db, order.id, technician, True)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()

    tracking.stopped_at = datetime.utcnow()
    assert tracking_session_state(order, tracking) == "ORPHANED"


def test_root_tracking_diagnostic_matches_public_projection_and_detects_inconsistency(db):
    org = make_org(db, "tracking-diagnostic")
    root = make_user(db, "root-diagnostic", "ROOT", org)
    manager = make_user(db, "manager-diagnostic", "GERENTE", org)
    technician = make_user(db, "technician-diagnostic", "BROKER", org)
    order = make_order(db, org, technician, supervisor=manager)
    order.service_request = ServiceRequest(
        organization_id=org.id,
        lead_id=order.lead_id,
        tracking_token="diagnostic-token",
        requester_name="Diagnostic Customer",
        service_category="TEST",
        status="ASSIGNED",
    )
    db.add(order.service_request)
    db.commit()
    start_tracking(db, order.id, technician, True)
    update_tracking_position(db, order.id, technician, 21.1610, -86.8505, 12)
    db.expire_all()

    diagnostic = diagnose_tracking_for_root(db, order.id, root)
    assert diagnostic["canonical"]["session_state"] == "ACTIVE"
    assert diagnostic["tracking"]["tracking_active"] is True
    assert diagnostic["tracking_record_count"] == 1
    assert diagnostic["tracking_service_order_unique_constraint"] is True
    assert diagnostic["public_projection"]["tracking_active"] is True
    assert diagnostic["public_projection"]["technician_display_name"] == technician.full_name
    assert "tracking_token" not in diagnostic["public_projection"]
    assert "MULTIPLE_TRACKING_RECORDS" not in diagnostic["inconsistencies"]

    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    tracking.stopped_at = datetime.utcnow()
    db.commit()
    db.expire_all()
    orphaned = diagnose_tracking_for_root(db, order.id, root)
    assert orphaned["canonical"]["session_state"] == "ORPHANED"
    assert "ACTIVE_WITH_STOPPED_AT" in orphaned["inconsistencies"]
    assert "PUBLIC_ADMIN_ACTIVE_MISMATCH" in orphaned["inconsistencies"]

    with pytest.raises(HTTPException) as blocked:
        diagnose_tracking_for_root(db, order.id, manager)
    assert blocked.value.status_code == 403
    with pytest.raises(HTTPException) as blocked:
        diagnose_tracking_for_root(db, order.id, technician)
    assert blocked.value.status_code == 403
    tracking.stopped_at = None
    order.status = "EM_ATENDIMENTO"
    assert tracking_session_state(order, tracking) == "ORPHANED"


def test_health_exposes_safe_deploy_sha_from_environment(monkeypatch):
    from app.main import health

    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("COMMIT_SHA", raising=False)
    monkeypatch.delenv("APP_VERSION", raising=False)
    assert health() == {"status": "online", "version": "unknown", "git_sha": "unknown"}

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123")
    assert health() == {"status": "online", "version": "abc123", "git_sha": "abc123"}


def test_cotizacion_enviada_is_not_startable_and_frontend_rule_matches_backend(db):
    org = make_org(db, "tracking-cotizacion")
    technician = make_user(db, "tecnico-cotizacion", "BROKER", org)
    order = make_order(db, org, technician, status="COTIZACAO_ENVIADA")

    assert order.tracking_start_allowed is False
    assert LeadResponse.model_validate(order.lead).service_order.tracking_start_allowed is False
    with pytest.raises(HTTPException) as blocked:
        start_tracking(db, order.id, technician, True)
    assert blocked.value.status_code == 409
    assert db.query(ServiceOrderTracking).count() == 0

    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    assert "order.tracking_start_allowed === true" in html
    assert "startableOrderStatuses" not in html


def test_only_assigned_technician_controls_tracking_and_cross_org_is_blocked(db):
    org_a = make_org(db, "tracking-org-a")
    org_b = make_org(db, "tracking-org-b")
    manager = make_user(db, "supervisor", "GERENTE", org_a)
    assigned = make_user(db, "assigned", "BROKER", org_a, manager_id=manager.id)
    other = make_user(db, "other", "BROKER", org_a)
    foreign = make_user(db, "foreign", "BROKER", org_b)
    order = make_order(db, org_a, assigned, manager)
    foreign_order = make_order(db, org_b, foreign)

    with pytest.raises(HTTPException) as blocked:
        start_tracking(db, order.id, other, True)
    assert blocked.value.status_code == 403
    with pytest.raises(HTTPException) as blocked:
        start_tracking(db, foreign_order.id, assigned, True)
    assert blocked.value.status_code in {403, 404}


@pytest.mark.parametrize(
    ("latitude", "longitude", "accuracy", "message"),
    [(91, 0, 1, "Latitude"), (0, 181, 1, "Longitude"), (0, 0, -1, "Precisao"), ("NaN", 0, 1, "Coordenadas")],
)
def test_invalid_tracking_positions_are_rejected(db, latitude, longitude, accuracy, message):
    org = make_org(db, f"tracking-invalid-{str(latitude).replace('.', '-')}")
    technician = make_user(db, "tecnico-invalido", "BROKER", org)
    order = make_order(db, org, technician)
    start_tracking(db, order.id, technician, True)

    with pytest.raises(HTTPException) as blocked:
        update_tracking_position(db, order.id, technician, latitude, longitude, accuracy)
    assert blocked.value.status_code == 400
    assert message in str(blocked.value.detail)


def test_position_requires_active_tracking_and_updates_single_row(db):
    org = make_org(db, "tracking-position")
    technician = make_user(db, "tecnico-posicion", "BROKER", org)
    order = make_order(db, org, technician)
    with pytest.raises(HTTPException) as blocked:
        update_tracking_position(db, order.id, technician, 21.1619, -86.8515, 8)
    assert blocked.value.status_code == 409

    start_tracking(db, order.id, technician, True)
    result = update_tracking_position(db, order.id, technician, 21.1619, -86.8515, 8)
    assert result["tracking"]["current_lat"] == pytest.approx(21.1619)
    assert result["tracking"]["current_lng"] == pytest.approx(-86.8515)
    assert db.query(ServiceOrderTracking).count() == 1
    assert db.query(LeadEvent).filter(LeadEvent.event_type == "TRACKING_POSITION_UPDATED").count() == 0


def test_tracking_health_distinguishes_live_stale_offline_and_finalized(db):
    org = make_org(db, "tracking-health")
    technician = make_user(db, "tecnico-health", "BROKER", org)
    order = make_order(db, org, technician)
    start_tracking(db, order.id, technician, True)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    now = datetime.utcnow()

    tracking.updated_at = now
    tracking.last_heartbeat_at = now
    tracking.current_lat = 21.16
    tracking.current_lng = -86.85
    assert tracking_health(tracking, now)["tracking_health"] == "LIVE"

    tracking.last_heartbeat_at = now
    tracking.updated_at = now - timedelta(seconds=40)
    assert tracking_health(tracking, now)["location_health"] == "STALE"
    tracking.updated_at = now - timedelta(seconds=121)
    assert tracking_health(tracking, now)["location_health"] == "OFFLINE"

    stop_tracking(db, order.id, technician, "MANUAL")
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    assert tracking_health(tracking, now)["tracking_health"] == "OFFLINE"


def test_offline_tracking_does_not_present_route_eta_as_current(db, monkeypatch):
    org = make_org(db, "tracking-route-health")
    technician = make_user(db, "tecnico-route-health", "BROKER", org)
    order = make_order(db, org, technician)
    order.location_lat = 21.2
    order.location_lng = -86.8
    start_tracking(db, order.id, technician, True)
    update_tracking_position(db, order.id, technician, 21.16, -86.85, 10)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    tracking.updated_at = datetime.utcnow() - timedelta(seconds=121)
    monkeypatch.setenv("ROUTING_PROVIDER_URL", "https://routing.test")
    assert route_for_order(order, tracking)["available"] is False


def test_heartbeat_refreshes_session_without_faking_gps_freshness(db):
    org = make_org(db, "tracking-heartbeat")
    technician = make_user(db, "tecnico-heartbeat", "BROKER", org)
    order = make_order(db, org, technician)
    start_tracking(db, order.id, technician, True)
    update_tracking_position(db, order.id, technician, 21.16, -86.85, 15)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    original_location_at = tracking.updated_at
    result = heartbeat_tracking(db, order.id, technician)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    assert result["tracking"]["last_heartbeat_at"] is not None
    assert tracking.updated_at == original_location_at
    assert tracking.current_lat == pytest.approx(21.16)

    with pytest.raises(HTTPException) as blocked:
        heartbeat_tracking(db, order.id, make_user(db, "other-heartbeat", "BROKER", org))
    assert blocked.value.status_code == 403


def test_tracking_diagnostics_are_scoped_and_do_not_accept_arbitrary_payloads(db):
    org = make_org(db, "tracking-diagnostics")
    technician = make_user(db, "tecnico-diagnostics", "BROKER", org)
    order = make_order(db, org, technician)
    start_tracking(db, order.id, technician, True)

    result = record_tracking_diagnostic(
        db,
        order.id,
        technician,
        "GPS_UPDATE_RECEIVED",
        accuracy_m=12,
        distance_m=42,
        coordinate_changed=True,
    )
    assert result == {"recorded": True, "event_type": "GPS_UPDATE_RECEIVED"}
    event = db.query(LeadEvent).filter(LeadEvent.event_type == "GPS_UPDATE_RECEIVED").one()
    assert "accuracy_m=12.0" in event.message
    assert "distance_m=42.0" in event.message
    with pytest.raises(HTTPException) as blocked:
        record_tracking_diagnostic(db, order.id, technician, "CLIENT_TOKEN")
    assert blocked.value.status_code == 400


def test_stop_arrived_records_event_and_stops_tracking(db):
    org = make_org(db, "tracking-arrived")
    manager = make_user(db, "manager-arrived", "GERENTE", org)
    technician = make_user(db, "tecnico-arrived", "BROKER", org, manager_id=manager.id)
    order = make_order(db, org, technician, manager)
    start_tracking(db, order.id, technician, True)

    result = stop_tracking(db, order.id, technician, "ARRIVED")
    assert result["status"] == "EM_ATENDIMENTO"
    assert result["tracking"]["tracking_active"] is False
    assert db.query(LeadEvent).filter(LeadEvent.event_type == "TECHNICIAN_ARRIVED").count() == 1


@pytest.mark.parametrize("reason", ["COMPLETED", "CANCELLED"])
def test_completed_and_cancelled_orders_stop_tracking(db, reason):
    org = make_org(db, f"tracking-{reason.lower()}")
    technician = make_user(db, f"tecnico-{reason.lower()}", "BROKER", org)
    order = make_order(db, org, technician)
    start_tracking(db, order.id, technician, True)

    stop_tracking_for_order(db, order, actor=None, reason=reason, preserve_status=True)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one()
    assert tracking.tracking_active is False
    assert tracking.stopped_at is not None


def test_root_and_supervisor_can_consult_but_regular_user_cannot(db):
    org = make_org(db, "tracking-consult")
    root = make_user(db, "root-consult", "ROOT", org)
    manager = make_user(db, "manager-consult", "GERENTE", org)
    technician = make_user(db, "technician-consult", "BROKER", org, manager_id=manager.id)
    outsider = make_user(db, "outsider-consult", "CLIENTE", org)
    order = make_order(db, org, technician, manager)
    start_tracking(db, order.id, technician, True)

    assert get_tracking_for_actor(db, order.id, root)["tracking"]["tracking_active"] is True
    assert get_tracking_for_actor(db, order.id, manager)["tracking"]["technician_id"] == technician.id
    with pytest.raises(HTTPException) as blocked:
        get_tracking_for_actor(db, order.id, outsider)
    assert blocked.value.status_code == 403


def test_operations_tracking_list_is_scoped_and_excludes_finished_routes(db):
    org_a = make_org(db, "tracking-operations-a")
    org_b = make_org(db, "tracking-operations-b")
    root = make_user(db, "root-operations", "ROOT", org_a)
    manager = make_user(db, "manager-operations", "GERENTE", org_a)
    technician = make_user(db, "technician-operations", "BROKER", org_a, manager_id=manager.id)
    foreign_technician = make_user(db, "foreign-operations", "BROKER", org_b)
    visible = make_order(db, org_a, technician, manager, status="ABERTA")
    visible.location_lat = 21.16
    visible.location_lng = -86.85
    hidden_org = make_order(db, org_b, foreign_technician, status="ABERTA")
    finished = make_order(db, org_a, technician, manager, status="COMPLETED")
    db.commit()
    start_tracking(db, visible.id, technician, True)
    start_tracking(db, hidden_org.id, foreign_technician, True)
    tracking = db.query(ServiceOrderTracking).filter_by(service_order_id=visible.id).one()
    tracking.current_lat = 21.17
    tracking.current_lng = -86.86
    db.commit()

    root_routes = list_active_tracking_for_actor(db, root)
    manager_routes = list_active_tracking_for_actor(db, manager)
    assert [item["service_order_id"] for item in root_routes] == [visible.id]
    assert [item["service_order_id"] for item in manager_routes] == [visible.id]
    assert root_routes[0]["technician"]["name"] == technician.full_name
    assert root_routes[0]["service_location"]["latitude"] == pytest.approx(21.16)
    assert root_routes[0]["tracking"]["current_lat"] == pytest.approx(21.17)

    stop_tracking_for_order(db, visible, actor=manager, reason="COMPLETED", preserve_status=True)
    assert list_active_tracking_for_actor(db, root) == []

    with pytest.raises(HTTPException) as blocked:
        list_active_tracking_for_actor(db, make_user(db, "client-operations", "CLIENTE", org_a))
    assert blocked.value.status_code == 403


def test_supervisor_sees_only_subordinate_tracking_inside_own_organization(db):
    org_a = make_org(db, "tracking-supervisor-a")
    org_b = make_org(db, "tracking-supervisor-b")
    supervisor_a = make_user(db, "supervisor-a", "GERENTE", org_a)
    technician_a = make_user(db, "technician-a", "BROKER", org_a, manager_id=supervisor_a.id)
    supervisor_b = make_user(db, "supervisor-b", "GERENTE", org_b)
    technician_b = make_user(db, "technician-b", "BROKER", org_b, manager_id=supervisor_b.id)
    order_a = make_order(db, org_a, technician_a, status="ABERTA")
    order_b = make_order(db, org_b, technician_b, status="ABERTA")
    for order in (order_a, order_b):
        order.location_lat = 21.16
        order.location_lng = -86.85
    db.commit()

    start_tracking(db, order_a.id, technician_a, True)
    start_tracking(db, order_b.id, technician_b, True)
    update_tracking_position(db, order_a.id, technician_a, 21.17, -86.86, 12)
    update_tracking_position(db, order_b.id, technician_b, 21.18, -86.87, 15)

    routes_a = list_active_tracking_for_actor(db, supervisor_a)
    routes_b = list_active_tracking_for_actor(db, supervisor_b)
    assert [route["technician"]["id"] for route in routes_a] == [technician_a.id]
    assert [route["technician"]["id"] for route in routes_b] == [technician_b.id]

    # The tracking record remains authoritative for the active technician even
    # if a legacy OS has lost its responsible-user snapshot.
    order_a.responsible_user_id = None
    db.commit()
    assert [route["technician"]["id"] for route in list_active_tracking_for_actor(db, supervisor_a)] == [technician_a.id]


def test_admin_stop_preserves_order_status_history_and_is_idempotent(db):
    org = make_org(db, "admin-stop-org")
    root = make_user(db, "admin-stop-root", "ROOT", org)
    technician = make_user(db, "admin-stop-tech", "BROKER", org)
    order = make_order(db, org, technician, status="ABERTA")
    order.location_lat = 21.16
    order.location_lng = -86.85
    db.commit()
    start_tracking(db, order.id, technician, True)
    update_tracking_position(db, order.id, technician, 21.17, -86.86, 10)

    result = admin_stop_tracking(db, order.id, root, "TEST_COMPLETED")
    assert result["status"] == "EN_CAMINO"
    assert result["tracking"]["tracking_active"] is False
    assert db.query(ServiceOrderTracking).filter_by(service_order_id=order.id).one().current_lat == pytest.approx(21.17)
    assert db.query(LeadEvent).filter_by(event_type="TRACKING_STOPPED").count() == 1

    again = admin_stop_tracking(db, order.id, root, "TEST_COMPLETED")
    assert again["status"] == "EN_CAMINO"
    assert db.query(LeadEvent).filter_by(event_type="TRACKING_STOPPED").count() == 1


def test_admin_stop_scope_and_stop_all_require_roles_and_preserve_status(db):
    org_a = make_org(db, "admin-stop-scope-a")
    org_b = make_org(db, "admin-stop-scope-b")
    root = make_user(db, "admin-scope-root", "ROOT", org_a)
    manager = make_user(db, "admin-scope-manager", "GERENTE", org_a)
    technician = make_user(db, "admin-scope-tech", "BROKER", org_a, manager_id=manager.id)
    foreign_tech = make_user(db, "admin-scope-foreign", "BROKER", org_b)
    order_a = make_order(db, org_a, technician, manager, status="ABERTA")
    order_b = make_order(db, org_b, foreign_tech, status="ABERTA")
    db.commit()
    start_tracking(db, order_a.id, technician, True)
    start_tracking(db, order_b.id, foreign_tech, True)

    with pytest.raises(HTTPException) as denied:
        admin_stop_tracking(db, order_a.id, technician, "OTHER")
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as foreign_denied:
        admin_stop_tracking(db, order_b.id, root, "OTHER")
    assert foreign_denied.value.status_code == 404
    with pytest.raises(HTTPException) as stop_all_denied:
        admin_stop_all_tracking(db, manager, "OTHER")
    assert stop_all_denied.value.status_code == 403

    stopped = admin_stop_all_tracking(db, root, "OPERATIONAL_CORRECTION")
    assert len(stopped) == 1
    assert db.query(ServiceOrder).filter_by(id=order_a.id).one().status == "EN_CAMINO"
    assert db.query(ServiceOrderTracking).filter_by(service_order_id=order_a.id).one().tracking_active is False
    assert db.query(ServiceOrderTracking).filter_by(service_order_id=order_b.id).one().tracking_active is True


def test_active_tracking_with_non_route_order_status_is_flagged_orphaned(db):
    org = make_org(db, "orphaned-route-org")
    root = make_user(db, "orphaned-route-root", "ROOT", org)
    technician = make_user(db, "orphaned-route-tech", "BROKER", org)
    order = make_order(db, org, technician, status="ABERTA")
    db.commit()
    start_tracking(db, order.id, technician, True)
    order.status = "EM_ATENDIMENTO"
    db.commit()

    routes = list_active_tracking_for_actor(db, root)
    assert len(routes) == 1
    assert routes[0]["tracking_state"] == "ORPHANED"
    assert routes[0]["tracking"]["tracking_active"] is True
