from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models import (
    lead,
    organization,
    organization_marketplace_link,
    service_order,
    service_order_completion,
    service_order_review,
    service_order_tracking,
    service_order_warranty_claim,
    service_property,
    service_request,
    user,
)
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance
from app.models.user import User
from app.services.service_order_review_service import calculate_nps, nps_summary, quality_projection, review_payload, submit_public_review


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[
        Organization.__table__,
        User.__table__,
        Lead.__table__,
        organization_marketplace_link.OrganizationMarketplaceLink.__table__,
        service_property.ServiceProperty.__table__,
        service_request.ServiceRequest.__table__,
        ServiceOrder.__table__,
        ServiceOrderCustomerAcceptance.__table__,
        service_order_completion.ServiceOrderWarranty.__table__,
        service_order_warranty_claim.ServiceOrderWarrantyClaim.__table__,
        service_order_review.ServiceOrderReview.__table__,
        service_order_tracking.ServiceOrderTracking.__table__,
    ])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def scenario(db, *, completed=True):
    org = Organization(name="Review Org", slug="review-org")
    other_org = Organization(name="Other Org", slug="other-review-org")
    db.add_all([org, other_org]); db.flush()
    customer = Lead(organization_id=org.id, nome="Customer", email="customer@example.com")
    manager = User(organization_id=org.id, username="manager-review", email="manager@example.com", password_hash="x", role="GERENTE", full_name="Manager")
    tech = User(organization_id=org.id, username="tech-review", email="tech@example.com", password_hash="x", role="BROKER", full_name="Tech")
    db.add_all([customer, manager, tech]); db.flush(); tech.manager_id = manager.id
    order = ServiceOrder(organization_id=org.id, lead_id=customer.id, order_number="TS-REVIEW", status="COMPLETED" if completed else "EN_CAMINO", responsible_user_id=tech.id, completed_at=datetime.utcnow() if completed else None)
    db.add(order); db.flush()
    acceptance = ServiceOrderCustomerAcceptance(organization_id=org.id, service_order_id=order.id, status="ACCEPTED", accepted_at=datetime.utcnow())
    db.add(acceptance); db.flush()
    return org, other_org, customer, manager, tech, order


def payload():
    return {"overall_rating": 5, "service_quality_rating": 4, "punctuality_rating": 5, "communication_rating": 4, "nps_score": 9, "comment": "Muito bom"}


def test_completed_customer_review_is_idempotent_and_does_not_change_order(db):
    _, _, _, _, _, order = scenario(db)
    first = submit_public_review(db, order, payload())
    duplicate = submit_public_review(db, order, {**payload(), "overall_rating": 1})
    db.commit()
    assert first.id == duplicate.id
    assert order.status == "COMPLETED"
    assert db.query(service_order_review.ServiceOrderReview).count() == 1
    assert review_payload(first, public=True)["nps_score"] == 9


def test_review_requires_completed_order_and_acceptance(db):
    _, _, _, _, _, order = scenario(db, completed=False)
    with pytest.raises(HTTPException) as exc:
        submit_public_review(db, order, payload())
    assert exc.value.status_code == 409
    db.delete(db.query(ServiceOrderCustomerAcceptance).filter_by(service_order_id=order.id).one()); db.flush()
    order.status = "COMPLETED"
    with pytest.raises(HTTPException) as exc:
        submit_public_review(db, order, payload())
    assert exc.value.status_code == 409


def test_quality_metrics_are_tenant_scoped_and_claims_are_independent(db):
    org, other_org, _, manager, tech, order = scenario(db)
    submit_public_review(db, order, payload()); db.commit()
    projection = quality_projection(db, manager)
    assert projection["organization"]["average_rating"] == 5.0
    assert projection["technicians"][0]["display_name"] == "Tech"
    assert projection["technicians"][0]["metrics"]["reviews_total"] == 1
    assert projection["organization"]["warranty_claims"] == 0
    assert projection["organization"]["nps"] == 100.0
    assert projection["organization"]["average_recommendation_score"] == 9.0
    assert projection["organization"]["warranty_claim_rate"] == 0.0
    foreign = User(organization_id=other_org.id, username="foreign-review", email="foreign@example.com", password_hash="x", role="GERENTE", full_name="Foreign")
    db.add(foreign); db.commit()
    assert quality_projection(db, foreign)["organization"]["reviews_total"] == 0
    assert order.status == "COMPLETED"


def test_claim_rate_counts_completed_services_not_claim_rows(db):
    org, _, customer, manager, tech, first_order = scenario(db)
    submit_public_review(db, first_order, payload())
    warranty = service_order_completion.ServiceOrderWarranty(
        organization_id=org.id,
        service_order_id=first_order.id,
        status="ACTIVE",
        warranty_days=90,
        starts_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=90),
    )
    db.add(warranty); db.flush()
    for index in range(2):
        db.add(service_order_warranty_claim.ServiceOrderWarrantyClaim(
            organization_id=org.id,
            warranty_id=warranty.id,
            service_order_id=first_order.id,
            customer_lead_id=customer.id,
            reason=f"Claim {index}",
            idempotency_key=f"claim-rate-{index}",
        ))
    second_order = ServiceOrder(
        organization_id=org.id, lead_id=customer.id, order_number="TS-REVIEW-2",
        status="COMPLETED", responsible_user_id=tech.id, completed_at=datetime.utcnow(),
    )
    db.add(second_order); db.commit()
    metrics = quality_projection(db, manager)["organization"]
    assert metrics["warranty_claim_count"] == 2
    assert metrics["warranty_claim_service_count"] == 1
    assert metrics["warranty_claim_rate"] == 50.0


def test_review_never_creates_financial_records(db):
    _, _, _, _, _, order = scenario(db)
    submit_public_review(db, order, payload())
    assert not hasattr(order, "payment")
    assert order.status == "COMPLETED"


def test_public_review_payload_contains_no_internal_identifiers(db):
    _, _, _, _, _, order = scenario(db)
    row = submit_public_review(db, order, payload())
    public = review_payload(row, public=True)
    assert public["submitted"] is True
    assert "id" not in public
    assert "organization_id" not in public
    assert "service_order_id" not in public
    assert "technician_user_id" not in public


def test_quality_projection_is_tenant_scoped_for_supervisors(db):
    org, other_org, _, manager, tech, order = scenario(db)
    submit_public_review(db, order, payload()); db.commit()
    other_manager = User(organization_id=other_org.id, username="other-manager", email="other-manager@example.com", password_hash="x", role="SUPERVISOR", full_name="Other manager")
    db.add(other_manager); db.commit()
    assert quality_projection(db, other_manager)["organization"]["reviews_total"] == 0
    assert all(item["display_name"] != "Tech" for item in quality_projection(db, other_manager)["technicians"])


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([9], 100.0),
        ([10, 9], 100.0),
        ([7, 8], 0.0),
        ([0], -100.0),
        ([6, 9], 0.0),
        ([5, 7, 10], 0.0),
        ([8, 9, 10], 66.67),
        ([None], None),
    ],
)
def test_nps_uses_promoters_and_detractors_not_average(scores, expected):
    assert calculate_nps(scores) == expected


def test_nps_summary_keeps_average_recommendation_separate():
    assert nps_summary([6, 7, 9]) == {
        "nps": 0.0,
        "average_recommendation_score": 7.33,
        "nps_responses": 3,
        "nps_promoters": 1,
        "nps_passives": 1,
        "nps_detractors": 1,
    }
