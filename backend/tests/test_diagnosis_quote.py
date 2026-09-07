from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models import organization, service_order, service_order_tracking, service_request, service_property, organization_marketplace_link, lead, user, service_order_diagnosis, service_order_quote
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.service_order_quote_service import (
    approve_public_quote,
    create_quote,
    latest_public_quote,
    public_quote_projection,
    reject_public_quote,
    scoped_order,
    upsert_diagnosis,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def scenario(db):
    one = Organization(name="A", slug="a")
    two = Organization(name="B", slug="b")
    db.add_all([one, two])
    db.flush()
    manager = User(organization_id=one.id, username="manager-a", email="manager-a@example.test", password_hash="x", role="GERENTE", full_name="Manager A")
    other = User(organization_id=two.id, username="manager-b", email="manager-b@example.test", password_hash="x", role="GERENTE", full_name="Manager B")
    tech = User(organization_id=one.id, username="tech-a", email="tech-a@example.test", password_hash="x", role="BROKER", full_name="Tech A", manager_id=None)
    db.add_all([manager, other, tech])
    db.flush()
    tech.manager_id = manager.id
    lead = Lead(organization_id=one.id, nome="Customer", tipo_servico="Plumbing")
    db.add(lead)
    db.flush()
    request = ServiceRequest(organization_id=one.id, lead_id=lead.id, tracking_token="token-071", service_category="Plumbing", requester_name="Customer")
    db.add(request)
    db.flush()
    order = ServiceOrder(organization_id=one.id, lead_id=lead.id, service_request_id=request.id, order_number="TS-071", status="ABERTA", responsible_user_id=tech.id)
    db.add(order)
    db.flush()
    return one, two, manager, other, tech, request, order


def test_diagnosis_and_quote_calculate_server_side_and_publish(db):
    one, _, manager, _, _, request, order = scenario(db)
    diagnosis = upsert_diagnosis(db, order, manager, {"problem_found": "Leak", "recommended_solution": "Replace valve", "observations": "Urgent"})
    quote = create_quote(db, order, manager, {"items": [{"description": "Labor", "quantity": Decimal("1"), "unit": "visit", "unit_price": Decimal("300")}, {"description": "Part", "quantity": Decimal("2"), "unit": "unit", "unit_price": Decimal("75")}], "discount_amount": Decimal("10"), "tax_amount": Decimal("20"), "currency": "mxn"})
    db.commit()
    assert diagnosis.problem_found == "Leak"
    assert quote.subtotal == Decimal("450.00")
    assert quote.total == Decimal("460.00")
    quote.status = "SENT"
    db.commit()
    public = public_quote_projection(db, order)
    assert public["diagnosis"]["problem_found"] == "Leak"
    assert public["quote"]["total"] == Decimal("460.00")
    assert "organization_id" not in public["quote"]


def test_public_approval_is_token_scoped_and_does_not_pay_or_change_order(db):
    one, two, manager, other, _, request, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Visit", "quantity": 1, "unit_price": 450}]})
    quote.status = "SENT"
    db.commit()
    approved = approve_public_quote(db, order)
    db.commit()
    assert approved.status == "APPROVED"
    assert approved.approved_source == "PUBLIC_TRACKING_TOKEN"
    assert approved.approved_total == Decimal("450.00")
    assert order.status == "ABERTA"
    with pytest.raises(HTTPException) as exc:
        approve_public_quote(db, order)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        scoped_order(db, order.id, other)
    assert exc.value.status_code == 404
    assert request.tracking_token == "token-071"
    assert one.id != two.id


def test_rejection_and_new_version_preserve_previous_economics(db):
    _, _, manager, _, _, _, order = scenario(db)
    first = create_quote(db, order, manager, {"items": [{"description": "Visit", "quantity": 1, "unit_price": 450}]})
    first.status = "SENT"
    db.commit()
    reject_public_quote(db, order, "Need another date")
    db.commit()
    assert first.status == "REJECTED"
    second = create_quote(db, order, manager, {"items": [{"description": "Visit", "quantity": 1, "unit_price": 500}]})
    db.commit()
    assert second.version == 2
    assert first.total == Decimal("450.00")
    assert second.total == Decimal("500.00")
    assert latest_public_quote(db, order.id, order.organization_id).id == first.id


def test_invalid_public_token_is_not_resolvable(db):
    from app.services.service_order_quote_service import resolve_public_order
    with pytest.raises(HTTPException) as exc:
        resolve_public_order(db, "invalid-token")
    assert exc.value.status_code == 404


def test_public_quote_copy_is_present_in_es_en_pt():
    html = (Path(__file__).parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    for phrase in (
        "Problema encontrado",
        "Aprobar presupuesto",
        "Rechazar presupuesto",
        "Problem found",
        "Approve quote",
        "Reject quote",
        "Problema encontrado",
        "Aprovar orçamento",
        "Recusar orçamento",
    ):
        assert phrase in html


def test_expired_quote_cannot_be_approved_publicly(db):
    _, _, manager, _, _, _, order = scenario(db)
    quote = create_quote(
        db,
        order,
        manager,
        {
            "items": [{"description": "Visit", "quantity": 1, "unit_price": 450}],
            "valid_until": datetime.utcnow() - timedelta(minutes=1),
        },
    )
    quote.status = "SENT"
    db.commit()

    with pytest.raises(HTTPException) as exc:
        approve_public_quote(db, order)

    assert exc.value.status_code == 409
    assert quote.status == "EXPIRED"
