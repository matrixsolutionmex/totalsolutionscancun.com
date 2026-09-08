from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models import (
    commercial_upgrade_intent, lead, organization, organization_marketplace_link, payment, service_order, service_order_completion,
    service_order_financial, service_order_quote, service_order_payment_plan, service_order_tracking,
    service_order_warranty_claim, service_property, service_request, user,
)
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderWarranty
from app.models.service_order_warranty_claim import ServiceOrderWarrantyClaim, ServiceOrderWarrantyClaimEvent
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.services.service_order_warranty_claim_service import (
    assign_claim, claim_payload, create_claim, customer_confirm, review_claim, update_claim,
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
    org_a, org_b = Organization(name="Warranty A", slug="warranty-a"), Organization(name="Warranty B", slug="warranty-b")
    db.add_all([org_a, org_b]); db.flush()
    manager = User(organization_id=org_a.id, username="w-manager", email="wm@test", password_hash="x", role="GERENTE", full_name="Manager A", email_verified=True, status="ACTIVE")
    tech = User(organization_id=org_a.id, username="w-tech", email="wt@test", password_hash="x", role="BROKER", full_name="Technician A", email_verified=True, status="ACTIVE")
    other = User(organization_id=org_b.id, username="w-other", email="wo@test", password_hash="x", role="GERENTE", full_name="Manager B", email_verified=True, status="ACTIVE")
    db.add_all([manager, tech, other]); db.flush(); tech.manager_id = manager.id
    lead = Lead(organization_id=org_a.id, nome="Warranty customer", tipo_servico="Plumbing")
    db.add(lead); db.flush()
    request = ServiceRequest(organization_id=org_a.id, lead_id=lead.id, tracking_token="warranty-token", service_category="Plumbing", requester_name="Warranty customer")
    db.add(request); db.flush()
    order = ServiceOrder(organization_id=org_a.id, lead_id=lead.id, service_request_id=request.id, order_number="TS-WARRANTY", status="COMPLETED", warranty_days=90, completed_at=datetime.utcnow())
    db.add(order); db.flush()
    warranty = ServiceOrderWarranty(organization_id=org_a.id, service_order_id=order.id, customer_lead_id=lead.id, warranty_days=90, starts_at=datetime.utcnow() - timedelta(days=1), ends_at=datetime.utcnow() + timedelta(days=89), scope="Repair")
    db.add(warranty); db.flush()
    return org_a, org_b, manager, tech, other, order, warranty


def test_claim_is_idempotent_and_does_not_create_financial_records(db):
    _, _, manager, _, _, order, _ = scenario(db)
    first = create_claim(db, order, reason="Leak returned", description="The same leak is back", evidence_reference="photo-1", idempotency_key="claim-1")
    duplicate = create_claim(db, order, reason="Changed", description=None, evidence_reference=None, idempotency_key="claim-1")
    db.commit()
    assert first.id == duplicate.id
    assert db.query(ServiceOrderWarrantyClaim).count() == 1
    assert db.query(ServiceOrderWarrantyClaimEvent).count() == 1
    assert order.status == "COMPLETED"
    assert db.query(payment.Payment).count() == 0
    assert db.query(service_order_financial.ServiceOrderFinancial).count() == 0
    assert claim_payload(first, public=True)["status"] == "open"


def test_claim_lifecycle_review_assign_resolve_and_customer_confirm(db):
    _, _, manager, tech, _, order, _ = scenario(db)
    claim = create_claim(db, order, reason="No cooling", description="Returned after repair", idempotency_key="claim-2")
    db.flush()
    assert review_claim(db, claim.id, manager, approve=True).status == "APPROVED"
    assert review_claim(db, claim.id, manager, approve=True).status == "APPROVED"
    assert assign_claim(db, claim.id, manager, tech.id).status == "TECHNICIAN_ASSIGNED"
    assert assign_claim(db, claim.id, manager, tech.id).status == "TECHNICIAN_ASSIGNED"
    assert update_claim(db, claim.id, tech, status="IN_PROGRESS").status == "IN_PROGRESS"
    assert update_claim(db, claim.id, tech, status="IN_PROGRESS").status == "IN_PROGRESS"
    assert update_claim(db, claim.id, tech, status="RESOLVED", notes="Repaired under warranty").status == "RESOLVED"
    assert update_claim(db, claim.id, tech, status="RESOLVED", notes="Repeated resolution").status == "RESOLVED"
    assert customer_confirm(db, order, claim.id, problem=False).status == "CLOSED"
    assert customer_confirm(db, order, claim.id, problem=False).status == "CLOSED"
    assert order.status == "COMPLETED"
    assert len(db.query(ServiceOrderWarrantyClaimEvent).filter_by(claim_id=claim.id).all()) == 6


def test_expired_warranty_and_wrong_tenant_are_blocked(db):
    _, _, _, _, other, order, warranty = scenario(db)
    warranty.ends_at = datetime.utcnow() - timedelta(seconds=1)
    with pytest.raises(HTTPException) as exc:
        create_claim(db, order, reason="Expired", idempotency_key="claim-expired")
    assert exc.value.status_code == 409
    warranty.ends_at = datetime.utcnow() + timedelta(days=1)
    claim = create_claim(db, order, reason="Valid", idempotency_key="claim-valid")
    db.flush()
    with pytest.raises(HTTPException) as exc:
        review_claim(db, claim.id, other, approve=True)
    assert exc.value.status_code == 404


def test_terminal_review_states_cannot_be_reopened_or_reversed(db):
    _, _, manager, _, _, order, _ = scenario(db)
    rejected = create_claim(db, order, reason="Rejected claim", idempotency_key="claim-rejected")
    db.flush()
    assert review_claim(db, rejected.id, manager, approve=False).status == "REJECTED"
    assert review_claim(db, rejected.id, manager, approve=False).status == "REJECTED"
    with pytest.raises(HTTPException) as exc:
        review_claim(db, rejected.id, manager, approve=True)
    assert exc.value.status_code == 409

    approved = create_claim(db, order, reason="Approved claim", idempotency_key="claim-approved")
    db.flush()
    assert review_claim(db, approved.id, manager, approve=True).status == "APPROVED"
    assert review_claim(db, approved.id, manager, approve=True).status == "APPROVED"
    with pytest.raises(HTTPException) as exc:
        review_claim(db, approved.id, manager, approve=False)
    assert exc.value.status_code == 409


def test_public_claim_payload_has_no_tenant_or_internal_assignment_fields(db):
    _, _, _, _, _, order, _ = scenario(db)
    claim = create_claim(db, order, reason="Noise", idempotency_key="claim-safe")
    payload = claim_payload(claim, public=True)
    assert payload["status"] == "open"
    assert "organization_id" not in payload
    assert "warranty_id" not in payload
    assert "service_order_id" not in payload
    assert "assigned_user_id" not in payload
