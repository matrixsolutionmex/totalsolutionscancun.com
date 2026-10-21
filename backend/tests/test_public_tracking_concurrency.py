import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - register every mapped table
from app.database.connection import Base
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import (
    ServiceOrderCustomerAcceptance,
    ServiceOrderTechnicalCompletion,
    ServiceOrderWarranty,
)
from app.models.service_order_warranty_claim import (
    ServiceOrderWarrantyClaim,
    ServiceOrderWarrantyClaimEvent,
)
from app.models.service_request import ServiceRequest
from app.models.user import User
from app.routes.public_service_request_routes import (
    PublicWarrantyClaimInput,
    public_completion_accept,
    public_completion_problem,
    public_warranty_claim,
)


@pytest.fixture()
def postgres_sessions(monkeypatch):
    database_url = os.getenv("PUBLIC_TRACKING_CONCURRENCY_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip(
            "PUBLIC_TRACKING_CONCURRENCY_TEST_DATABASE_URL is required for row-lock concurrency checks"
        )
    monkeypatch.setenv("AUTH_AUDIT_PEPPER", "synthetic-public-tracking-concurrency-pepper")
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        engine.dispose()


def _seed_action(sessions, action: str) -> tuple[int, str]:
    with sessions() as db:
        organization = Organization(name=f"Concurrency {action}", slug=f"concurrency-{action}")
        db.add(organization)
        db.flush()
        user = User(
            organization_id=organization.id,
            username=f"concurrency-{action}",
            email=f"concurrency-{action}@example.test",
            password_hash="x",
            role="BROKER",
            full_name="Synthetic technician",
        )
        lead = Lead(organization_id=organization.id, nome="Synthetic customer")
        db.add_all([user, lead])
        db.flush()
        request = ServiceRequest(
            organization_id=organization.id,
            lead_id=lead.id,
            tracking_token=f"synthetic-concurrent-{action}-token",
            service_category="Synthetic",
            requester_name="Synthetic customer",
        )
        db.add(request)
        db.flush()
        order = ServiceOrder(
            organization_id=organization.id,
            lead_id=lead.id,
            service_request_id=request.id,
            order_number=f"TS-CONCURRENT-{action.upper()}",
            status="COMPLETED" if action == "warranty" else "ABERTA",
            responsible_user_id=user.id,
            warranty_days=90,
        )
        db.add(order)
        db.flush()
        if action in {"accept", "problem"}:
            db.add(ServiceOrderTechnicalCompletion(
                organization_id=organization.id,
                service_order_id=order.id,
                responsible_user_id=user.id,
                status="REPORTED",
            ))
        else:
            db.add(ServiceOrderWarranty(
                organization_id=organization.id,
                service_order_id=order.id,
                customer_lead_id=lead.id,
                warranty_days=90,
                starts_at=datetime.utcnow() - timedelta(days=1),
                ends_at=datetime.utcnow() + timedelta(days=89),
                scope="Synthetic repair",
            ))
        db.commit()
        return order.id, request.tracking_token


@pytest.mark.parametrize("action", ["accept", "problem", "warranty"])
def test_public_action_concurrency_creates_one_effect_per_order(postgres_sessions, action):
    order_id, token = _seed_action(postgres_sessions, action)
    barrier = Barrier(2)

    def invoke():
        with postgres_sessions() as db:
            barrier.wait(timeout=5)
            if action == "accept":
                public_completion_accept(token, db=db)
            elif action == "problem":
                public_completion_problem(token, {"reason": "Synthetic problem"}, db=db)
            else:
                public_warranty_claim(
                    token,
                    PublicWarrantyClaimInput(
                        reason="Synthetic warranty",
                        idempotency_key="synthetic-client-retry",
                    ),
                    db=db,
                )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)

    with postgres_sessions() as db:
        if action in {"accept", "problem"}:
            rows = db.query(ServiceOrderCustomerAcceptance).filter_by(service_order_id=order_id).all()
            assert len(rows) == 1
            assert rows[0].status == ("ACCEPTED" if action == "accept" else "PROBLEM_REPORTED")
            if action == "accept":
                assert db.query(ServiceOrderWarranty).filter_by(service_order_id=order_id).count() == 1
        else:
            claims = db.query(ServiceOrderWarrantyClaim).filter_by(service_order_id=order_id).all()
            assert len(claims) == 1
            assert db.query(ServiceOrderWarrantyClaimEvent).filter_by(claim_id=claims[0].id).count() == 1
