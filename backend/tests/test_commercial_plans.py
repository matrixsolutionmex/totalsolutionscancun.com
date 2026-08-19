import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.commercial_subscription import CommercialSubscription, PlanChangeEvent
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.routes.commercial_routes import MockPlanChangeRequest, commercial_mock_plan
from app.services.entitlement_service import account_snapshot, can_use_feature, get_plan_limits, plan_catalog


@pytest.fixture()
def commercial_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[Organization.__table__, User.__table__, Lead.__table__, ServiceOrder.__table__, ServiceOpportunity.__table__, CommercialSubscription.__table__, PlanChangeEvent.__table__])
    db = sessionmaker(bind=engine)()
    org = Organization(name="Commercial Org", slug="commercial-org")
    other_org = Organization(name="Other Commercial Org", slug="other-commercial-org")
    db.add_all([org, other_org])
    db.flush()
    actor = User(username="commercial-root", full_name="Root", password_hash="hash", role="ROOT", organization_id=org.id, status="ACTIVE", is_active=True)
    broker = User(username="commercial-broker", full_name="Broker", password_hash="hash", role="BROKER", organization_id=org.id, status="ACTIVE", is_active=True)
    other = User(username="other-broker", full_name="Other", password_hash="hash", role="BROKER", organization_id=other_org.id, status="ACTIVE", is_active=True)
    db.add_all([actor, broker, other])
    db.commit()
    yield db, actor, broker, other
    db.close()


def test_catalog_has_free_pro_business_and_central_limits(commercial_db):
    plans = {plan["name"]: plan for plan in plan_catalog()}
    assert set(plans) == {"FREE", "PRO", "BUSINESS"}
    assert plans["FREE"]["monthly_reference"] == 0
    assert plans["PRO"]["monthly_reference"] == 499
    assert plans["BUSINESS"]["monthly_reference"] == 1999
    assert plans["PRO"]["recommended"] is True


def test_mock_upgrade_is_authorized_audited_and_isolated(commercial_db):
    db, actor, broker, other = commercial_db
    result = commercial_mock_plan(MockPlanChangeRequest(plan="PRO", reason="teste promocional"), db, actor)
    assert result["plan"] == "PRO"
    assert result["status"] == "LAUNCH_ACCESS"
    snapshot = account_snapshot(db, broker)
    assert snapshot["plan"] == "PRO"
    assert snapshot["launch_message"] == "Acesso promocional liberado. Pagamento online em breve."
    assert db.query(PlanChangeEvent).filter(PlanChangeEvent.organization_id == actor.organization_id).count() == 1
    assert account_snapshot(db, other)["plan"] == "FREE"


def test_broker_cannot_forge_paid_plan(commercial_db):
    db, actor, broker, _ = commercial_db
    with pytest.raises(Exception) as error:
        commercial_mock_plan(MockPlanChangeRequest(plan="BUSINESS"), db, broker)
    assert "autorização administrativa" in str(error.value)
    assert account_snapshot(db, broker)["plan"] == "FREE"


def test_entitlements_and_limits_are_centralized(commercial_db):
    db, actor, broker, _ = commercial_db
    assert can_use_feature(db, broker, "RADAR")
    assert not can_use_feature(db, broker, "PABLO_FULL")
    assert get_plan_limits(db, broker)["radar_opportunities"] == 5
    commercial_mock_plan(MockPlanChangeRequest(plan="BUSINESS"), db, actor)
    assert can_use_feature(db, broker, "TEAM_MANAGEMENT")
    assert get_plan_limits(db, broker)["users"] == 100
