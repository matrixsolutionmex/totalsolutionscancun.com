from datetime import datetime

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
from app.services.service_order_tracking_service import (
    get_tracking_for_actor,
    list_active_tracking_for_actor,
    start_tracking,
    stop_tracking,
    stop_tracking_for_order,
    update_tracking_position,
)


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
    visible = make_order(db, org_a, technician, manager, status="EN_CAMINO")
    visible.location_lat = 21.16
    visible.location_lng = -86.85
    hidden_org = make_order(db, org_b, foreign_technician, status="EN_CAMINO")
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
