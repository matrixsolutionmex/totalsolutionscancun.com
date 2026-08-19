import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.auth_security import AuthAuditEvent
from app.models.commercial_subscription import CommercialSubscription, PlanChangeEvent
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.service_request import ServiceRequest
from app.models.service_property import ServiceProperty
from app.models.user import User
from app.routes.commercial_routes import MockPlanChangeRequest, UpgradeIntentRequest, commercial_mock_plan, commercial_upgrade_intent
from app.services.commercial_upgrade_service import activate_upgrade_intent, create_upgrade_intent, mark_payment_confirmed
from app.services.entitlement_service import account_snapshot, can_use_feature, get_plan_limits, plan_catalog


@pytest.fixture()
def commercial_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[Organization.__table__, User.__table__, Lead.__table__, ServiceProperty.__table__, ServiceRequest.__table__, ServiceOrder.__table__, ServiceOpportunity.__table__, CommercialSubscription.__table__, PlanChangeEvent.__table__, CommercialUpgradeIntent.__table__, AuthAuditEvent.__table__])
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


def test_pro_upgrade_intent_uses_catalog_price_and_does_not_activate(commercial_db):
    db, actor, _, _ = commercial_db
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="PRO"), None, db, actor)
    intent = db.query(CommercialUpgradeIntent).filter_by(id=result["intent_id"]).one()
    assert result == {
        "intent_id": intent.id,
        "plan": "PRO",
        "provider": "CLIP",
        "checkout_url": "https://pago.clip.mx/v3/5210f95c-eb87-4c85-bdd6-d4d84c7255d0",
        "status": "CHECKOUT_OPENED",
    }
    assert float(intent.reference_price_mxn) == 499.0
    assert intent.organization_id == actor.organization_id
    assert db.query(CommercialSubscription).filter_by(organization_id=actor.organization_id).count() == 0
    assert db.query(AuthAuditEvent).filter_by(event_type="UPGRADE_INTENT_CREATED").count() == 1


def test_business_upgrade_intent_ignores_untrusted_price_and_scope(commercial_db):
    db, _, broker, other = commercial_db
    with pytest.raises(ValidationError):
        UpgradeIntentRequest(plan="BUSINESS", price=1, organization_id=999)
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="BUSINESS"), None, db, other)
    intent = db.query(CommercialUpgradeIntent).filter_by(id=result["intent_id"]).one()
    assert float(intent.reference_price_mxn) == 1999.0
    assert intent.organization_id == other.organization_id
    assert intent.user_id == other.id


def test_broker_cannot_confirm_or_activate_own_upgrade_intent(commercial_db):
    db, _, broker, _ = commercial_db
    intent = create_upgrade_intent(db, broker, "PRO")
    with pytest.raises(Exception) as error:
        mark_payment_confirmed(db, broker, intent.id)
    assert getattr(error.value, "status_code", None) == 403
    assert db.query(CommercialSubscription).filter_by(organization_id=broker.organization_id).count() == 0


def test_admin_confirmation_then_activation_reuses_billing_service(commercial_db):
    db, actor, _, _ = commercial_db
    intent = create_upgrade_intent(db, actor, "BUSINESS")
    confirmed = mark_payment_confirmed(db, actor, intent.id)
    assert confirmed.status == "PAID"
    activated, subscription = activate_upgrade_intent(db, actor, intent.id)
    assert activated.status == "ACTIVATED"
    assert subscription.plan == "BUSINESS"
    assert account_snapshot(db, actor)["plan"] == "BUSINESS"
    assert db.query(PlanChangeEvent).filter_by(organization_id=actor.organization_id).count() == 1
    assert db.query(AuthAuditEvent).filter_by(event_type="UPGRADE_PAYMENT_CONFIRMED").count() == 1
    assert db.query(AuthAuditEvent).filter_by(event_type="UPGRADE_PLAN_ACTIVATED").count() == 1


def test_checkout_opened_does_not_change_subscription_and_orgs_are_isolated(commercial_db):
    db, actor, _, other = commercial_db
    first = create_upgrade_intent(db, actor, "PRO")
    second = create_upgrade_intent(db, other, "BUSINESS")
    assert account_snapshot(db, actor)["plan"] == "FREE"
    assert account_snapshot(db, other)["plan"] == "FREE"
    with pytest.raises(Exception) as error:
        activate_upgrade_intent(db, actor, second.id)
    assert getattr(error.value, "status_code", None) == 404
    assert first.organization_id != second.organization_id
