from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.auth_security import AuthAuditEvent
from app.models import (
    commercial_upgrade_intent,
    lead,
    organization,
    organization_marketplace_link,
    organization_payment_policy,
    payment,
    service_order,
    service_order_completion,
    service_order_diagnosis,
    service_order_financial,
    service_order_installment_release_event,
    service_order_ledger_entry,
    service_order_payment_plan,
    service_order_quote,
    service_order_tracking,
    service_property,
    service_request,
    user,
    visit_pricing_snapshot,
)
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance, ServiceOrderWarranty
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.service_order_completion_service import (
    completion_projection,
    customer_acceptance,
    customer_report_problem,
    record_technical_completion,
    review_technical_completion,
)
from app.services.service_order_payment_plan_service import create_payment_plan_for_approved_quote
from app.services.service_order_quote_service import create_quote


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
    org_a, org_b = Organization(name="A", slug="completion-a"), Organization(name="B", slug="completion-b")
    db.add_all([org_a, org_b])
    db.flush()
    manager = User(organization_id=org_a.id, username="manager", email="manager@test", password_hash="x", role="GERENTE", full_name="Manager")
    tech = User(organization_id=org_a.id, username="tech", email="tech@test", password_hash="x", role="BROKER", full_name="Tech")
    other = User(organization_id=org_b.id, username="other", email="other@test", password_hash="x", role="GERENTE", full_name="Other")
    db.add_all([manager, tech, other])
    db.flush()
    tech.manager_id = manager.id
    lead = Lead(organization_id=org_a.id, nome="Customer", tipo_servico="Plumbing")
    db.add(lead)
    db.flush()
    request = ServiceRequest(organization_id=org_a.id, lead_id=lead.id, tracking_token="completion-token", service_category="Plumbing", requester_name="Customer")
    db.add(request)
    db.flush()
    order = ServiceOrder(organization_id=org_a.id, lead_id=lead.id, service_request_id=request.id, order_number="TS-COMPLETION", status="ABERTA", responsible_user_id=tech.id, warranty_days=90)
    db.add(order)
    db.flush()
    return org_a, org_b, manager, tech, other, order


def approved_plan(db, order, manager, total):
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": total}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    db.flush()
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    return plan


def test_technical_completion_is_idempotent_and_does_not_finish_order(db):
    _, _, manager, tech, other, order = scenario(db)
    first = record_technical_completion(db, order, tech, {"completion_notes": "Finished", "final_observation": "OK"})
    same = record_technical_completion(db, order, tech, {"completion_notes": "ignored"})
    db.commit()
    assert first.id == same.id
    assert order.status == "ABERTA"
    assert order.completed_at is None
    with pytest.raises(HTTPException) as exc:
        record_technical_completion(db, order, other, {})
    assert exc.value.status_code == 404
    reviewed = review_technical_completion(db, order, manager)
    assert reviewed.status == "REVIEWED"
    assert order.status == "ABERTA"


def test_technician_cannot_review_technical_completion(db):
    _, _, _, tech, _, order = scenario(db)
    record_technical_completion(db, order, tech, {"completion_notes": "Ready"})
    with pytest.raises(HTTPException) as exc:
        review_technical_completion(db, order, tech)
    assert exc.value.status_code == 403
    assert order.status == "ABERTA"


def test_acceptance_requires_settled_service_and_starts_warranty_once(db):
    _, _, manager, tech, _, order = scenario(db)
    plan = approved_plan(db, order, manager, Decimal("2800"))
    record_technical_completion(db, order, tech, {"completion_notes": "Ready", "final_observation": "OK"})
    with pytest.raises(HTTPException) as exc:
        customer_acceptance(db, order, idempotency_key="accept-1")
    assert exc.value.status_code == 409
    plan.installments[0].status = "PAID"
    accepted = customer_acceptance(db, order, idempotency_key="accept-1")
    duplicate = customer_acceptance(db, order, idempotency_key="accept-1")
    db.commit()
    assert accepted.id == duplicate.id
    assert order.status == "COMPLETED"
    assert db.query(ServiceOrderCustomerAcceptance).count() == 1
    assert db.query(ServiceOrderWarranty).count() == 1
    warranty = db.query(ServiceOrderWarranty).one()
    assert warranty.customer_lead_id == order.lead_id
    assert warranty.scope == "OK"
    assert completion_projection(db, order, public=True)["receipt"]["order_number"] == "TS-COMPLETION"
    assert completion_projection(db, order, public=True)["warranty"]["active"] is True
    audit = db.query(AuthAuditEvent).filter_by(event_type="SERVICE_ORDER_CUSTOMER_ACCEPTED").one()
    assert '"status_before": "ABERTA"' in audit.detail
    assert '"status_after": "COMPLETED"' in audit.detail
    assert '"quote_version": 1' in audit.detail


def test_problem_report_is_idempotent_and_does_not_change_operation(db):
    _, _, _, tech, _, order = scenario(db)
    record_technical_completion(db, order, tech, {"completion_notes": "Ready"})
    first = customer_report_problem(db, order, reason="Still leaking", idempotency_key="problem-1")
    same = customer_report_problem(db, order, reason="changed", idempotency_key="problem-1")
    db.commit()
    assert first.id == same.id
    assert first.status == "PROBLEM_REPORTED"
    assert first.problem_reason == "Still leaking"
    assert order.status == "ABERTA"
    projection = completion_projection(db, order, public=True)
    assert projection["customer_status"] == "problem_reported"
    assert projection["ready_for_acceptance"] is False
    assert "organization_id" not in projection


def test_completion_projection_is_tenant_scoped_and_public_is_safe(db):
    _, _, manager, _, other, order = scenario(db)
    record_technical_completion(db, order, manager, {"completion_notes": "Done", "evidence_reference": "photo-1"})
    assert completion_projection(db, order, public=True)["technical_complete"] is True
    with pytest.raises(HTTPException) as exc:
        from app.services.service_order_quote_service import scoped_order
        scoped_order(db, order.id, other)
    assert exc.value.status_code == 404
    assert "responsible_user_id" not in completion_projection(db, order, public=True)


@pytest.mark.parametrize("total,paid_sequences", [(Decimal("10000"), {1}), (Decimal("30000"), {1})])
def test_customer_acceptance_blocks_with_future_installments_pending(db, total, paid_sequences):
    _, _, manager, tech, _, order = scenario(db)
    plan = approved_plan(db, order, manager, total)
    for installment in plan.installments:
        if installment.sequence in paid_sequences:
            installment.status = "PAID"
    record_technical_completion(db, order, tech, {"completion_notes": "Ready"})
    with pytest.raises(HTTPException) as exc:
        customer_acceptance(db, order, idempotency_key=f"blocked-{total}")
    assert exc.value.status_code == 409
    assert order.status == "ABERTA"
    assert completion_projection(db, order, public=True)["ready_for_acceptance"] is False
