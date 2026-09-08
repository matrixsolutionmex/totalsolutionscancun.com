from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models import (
    commercial_upgrade_intent,
    lead,
    organization,
    organization_marketplace_link,
    payment,
    service_order,
    service_order_completion,
    service_order_diagnosis,
    service_order_financial,
    service_order_installment_release_event,
    service_order_ledger_entry,
    service_order_payment_plan,
    service_order_quote,
    service_order_review,
    service_order_payment_plan,
    service_order_tracking,
    service_order_warranty_claim,
    service_property,
    service_request,
    technician_skill,
    user,
    visit_pricing_snapshot,
)
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.technician_recommendation_service import recommend_technicians, validate_recommended_assignment


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_user(db, org, username, role, name, manager_id=None):
    row = User(
        organization_id=org.id if org else None,
        username=username,
        email=f"{username}@example.com",
        password_hash="x",
        role=role,
        full_name=name,
        manager_id=manager_id,
        status="ACTIVE",
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def make_order(db, org, category="PLUMBING", number="TS-077"):
    customer = Lead(organization_id=org.id, nome=f"Customer {number}", email=f"{number}@example.com")
    db.add(customer)
    db.flush()
    request = ServiceRequest(
        organization_id=org.id,
        lead_id=customer.id,
        tracking_token=f"token-{number}",
        requester_name="Customer",
        service_category=category,
        location_lat=21.16,
        location_lng=-86.85,
    )
    db.add(request)
    db.flush()
    order = ServiceOrder(
        organization_id=org.id,
        lead_id=customer.id,
        service_request_id=request.id,
        order_number=number,
        status="ABERTA",
        location_lat=21.17,
        location_lng=-86.84,
    )
    db.add(order)
    db.flush()
    return order


def add_skill(db, org, technician, value):
    db.add(technician_skill.TechnicianSkill(
        organization_id=org.id,
        technician_user_id=technician.id,
        skill=value,
        active=True,
    ))


def scenario(db):
    org_a = Organization(name="Org A", slug="org-a")
    org_b = Organization(name="Org B", slug="org-b")
    db.add_all([org_a, org_b])
    db.flush()
    manager_a = make_user(db, org_a, "manager-a", "GERENTE", "Manager A")
    manager_b = make_user(db, org_b, "manager-b", "GERENTE", "Manager B")
    tech_a = make_user(db, org_a, "tech-a", "BROKER", "Technician A", manager_a.id)
    tech_a_wrong_skill = make_user(db, org_a, "tech-a-electric", "BROKER", "Electrician A", manager_a.id)
    tech_b = make_user(db, org_b, "tech-b", "BROKER", "Technician B", manager_b.id)
    add_skill(db, org_a, tech_a, "PLUMBING")
    add_skill(db, org_a, tech_a_wrong_skill, "ELECTRICAL")
    add_skill(db, org_b, tech_b, "PLUMBING")
    order_a = make_order(db, org_a, number="TS-077-A")
    order_b = make_order(db, org_b, number="TS-077-B")
    db.commit()
    return org_a, org_b, manager_a, manager_b, tech_a, tech_a_wrong_skill, tech_b, order_a, order_b


def test_manager_recommendations_are_hierarchical_and_tenant_scoped(db):
    _, _, manager_a, manager_b, tech_a, wrong_skill, tech_b, order_a, order_b = scenario(db)

    result_a = recommend_technicians(db, order_a.id, manager_a)
    assert [item["technician"]["id"] for item in result_a["candidates"]] == [tech_a.id]
    assert all(item["technician"]["id"] != tech_b.id for item in result_a["candidates"])
    assert all(item["technician"]["id"] != wrong_skill.id for item in result_a["candidates"])

    result_b = recommend_technicians(db, order_b.id, manager_b)
    assert [item["technician"]["id"] for item in result_b["candidates"]] == [tech_b.id]
    with pytest.raises(HTTPException) as exc:
        recommend_technicians(db, order_b.id, manager_a)
    assert exc.value.status_code == 404


def test_root_can_view_order_tenant_without_leaking_candidates_between_orders(db):
    org_a, _, _, _, tech_a, _, tech_b, order_a, order_b = scenario(db)
    root = make_user(db, org_a, "root", "ROOT", "Root")
    db.commit()

    assert [item["technician"]["id"] for item in recommend_technicians(db, order_a.id, root)["candidates"]] == [tech_a.id]
    assert [item["technician"]["id"] for item in recommend_technicians(db, order_b.id, root)["candidates"]] == [tech_b.id]


def test_recommendation_does_not_assign_and_revalidation_rejects_busy_technician(db):
    org_a, _, manager_a, _, tech_a, _, _, order_a, _ = scenario(db)
    busy_order = make_order(db, org_a, number="TS-077-BUSY")
    busy_order.responsible_user_id = tech_a.id
    busy_order.status = "EN_CAMINO"
    db.commit()

    result = recommend_technicians(db, order_a.id, manager_a)
    assert result["candidates"] == []
    busy = next(item for item in result["excluded"] if item["technician"]["id"] == tech_a.id)
    assert busy["availability"] == "BUSY"
    assert order_a.responsible_user_id is None
    with pytest.raises(HTTPException) as exc:
        validate_recommended_assignment(db, order_a, tech_a.id, manager_a)
    assert exc.value.status_code == 409


def test_recommendation_handles_missing_coordinates_and_explains_unknown_skill(db):
    org_a, _, manager_a, _, _, _, _, order_a, _ = scenario(db)
    order_a.location_lat = None
    order_a.location_lng = None
    order_a.service_request.location_lat = None
    order_a.service_request.location_lng = None
    db.commit()

    result = recommend_technicians(db, order_a.id, manager_a)
    candidate = result["candidates"][0]
    assert candidate["distance_km"] is None
    assert any("coordenadas" in reason for reason in candidate["reasons"])
    assert candidate["skill_match"] == "STRONG"
    assert result["service_order_id"] == order_a.id
