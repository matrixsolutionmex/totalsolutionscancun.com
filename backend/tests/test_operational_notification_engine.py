import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database.connection import Base
from app.models.notification import Notification
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.service_order_tracking import ServiceOrderTracking
from app.models.service_property import ServiceProperty
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.operational_notification_service import (
    OperationalEvent,
    emit_operational_notification,
    get_eligible_technicians_for_opportunity,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[
        Organization.__table__, User.__table__, Lead.__table__, ServiceProperty.__table__,
        ServiceRequest.__table__, ServiceOrder.__table__, ServiceOrderTracking.__table__,
        ServiceOpportunity.__table__, Notification.__table__,
    ])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_org(db, slug):
    organization = Organization(name=slug.title(), slug=slug)
    db.add(organization)
    db.commit()
    db.refresh(organization)
    return organization


def make_user(db, username, role, organization_id, *, active=True, status="ACTIVE"):
    user = User(
        username=username,
        email=f"{username}@example.test",
        full_name=username.title(),
        password_hash=hash_password("secret"),
        role=role,
        organization_id=organization_id,
        is_active=active,
        status=status,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_opportunity(db, organization_id):
    opportunity = ServiceOpportunity(
        public_id="MKT-NOTIFICATION-001",
        organization_id=organization_id,
        service_type="Plomería",
        segment="RESIDENCIAL",
        city="Cancún",
        urgency="ALTA",
        pricing_zone="Z1",
        customer_budget_min=1000,
        customer_budget_max=1450,
        market_reference_min=450,
        market_reference_max=1800,
        visit_calculated_price=500,
        pricing_currency="MXN",
        pricing_version="CANCUN_V1",
        status="AVAILABLE",
    )
    db.add(opportunity)
    db.commit()
    db.refresh(opportunity)
    return opportunity


def test_marketplace_created_notifies_admins_and_eligible_technicians(db):
    org = make_org(db, "notification-org")
    other_org = make_org(db, "other-notification-org")
    root = make_user(db, "root", "ROOT", org.id)
    manager = make_user(db, "manager", "GERENTE", org.id)
    technician = make_user(db, "technician", "BROKER", org.id)
    inactive = make_user(db, "inactive", "BROKER", org.id, active=False, status="SUSPENDED")
    other_technician = make_user(db, "other-tech", "BROKER", other_org.id)
    opportunity = make_opportunity(db, org.id)

    ids = emit_operational_notification(
        db,
        event_type=OperationalEvent.MARKETPLACE_SERVICE_CREATED,
        organization_id=org.id,
        opportunity_id=opportunity.id,
    )
    db.commit()

    notifications = db.query(Notification).all()
    assert len(ids) == 3
    assert {item.recipient_user_id for item in notifications} == {root.id, manager.id, technician.id}
    assert inactive.id not in {item.recipient_user_id for item in notifications}
    assert other_technician.id not in {item.recipient_user_id for item in notifications}
    assert all(item.type == "marketplace_service_created" for item in notifications)
    assert all(item.priority == "NORMAL" for item in notifications)


def test_marketplace_notification_is_deduplicated_per_recipient(db):
    org = make_org(db, "dedupe-notification-org")
    root = make_user(db, "root-dedupe", "ROOT", org.id)
    opportunity = make_opportunity(db, org.id)

    first = emit_operational_notification(
        db, event_type="MARKETPLACE_SERVICE_CREATED", organization_id=org.id, opportunity_id=opportunity.id
    )
    second = emit_operational_notification(
        db, event_type="MARKETPLACE_SERVICE_CREATED", organization_id=org.id, opportunity_id=opportunity.id
    )
    db.commit()

    assert first == second
    assert db.query(Notification).filter(Notification.recipient_user_id == root.id).count() == 1


def test_marketplace_notification_metadata_is_sanitized(db):
    org = make_org(db, "sanitized-notification-org")
    root = make_user(db, "root-sanitized", "ROOT", org.id)
    opportunity = make_opportunity(db, org.id)

    emit_operational_notification(
        db,
        event_type=OperationalEvent.MARKETPLACE_SERVICE_CREATED,
        organization_id=org.id,
        opportunity_id=opportunity.id,
        metadata={
            "email": "cliente@example.com",
            "phone": "+52 998 555 0000",
            "address": "Dirección privada 123",
            "safe_note": "diagnóstico pendiente",
        },
    )
    db.commit()
    notification = db.query(Notification).filter(Notification.recipient_user_id == root.id).one()
    serialized = notification.metadata_json
    assert "cliente@example.com" not in serialized
    assert "+52 998 555 0000" not in serialized
    assert "Dirección privada 123" not in serialized
    assert json.loads(serialized)["safe_note"] == "diagnóstico pendiente"
    assert notification.lead_id is None
    assert "tracking_token" not in serialized


def test_eligible_technicians_are_same_org_active_brokers_only(db):
    org = make_org(db, "eligible-notification-org")
    make_user(db, "eligible", "BROKER", org.id)
    make_user(db, "manager-not-technical", "GERENTE", org.id)
    make_user(db, "pending", "BROKER", org.id, status="PENDING_ADMIN")
    other_org = make_org(db, "eligible-other-org")
    make_user(db, "other", "BROKER", other_org.id)
    opportunity = make_opportunity(db, org.id)

    assert [user.username for user in get_eligible_technicians_for_opportunity(db, opportunity)] == ["eligible"]


def test_frontend_keeps_seen_notification_guard_and_priority_states():
    html = Path(__file__).resolve().parents[2].joinpath("frontend/index.html").read_text()
    assert "seenNotificationIds.has(notification.id)" in html
    assert "priority-high" in html
    assert "priority-critical" in html
    assert "notification.priority || \"NORMAL\"" in html
