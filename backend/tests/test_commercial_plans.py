import json
from pathlib import Path
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.auth_security import AuthAuditEvent
from app.models.commercial_subscription import CommercialSubscription, PlanChangeEvent
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.payment import Payment, PlatformLedgerEntry
from app.models.notification import Notification, NotificationPreference, WebPushSubscription
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.organization_marketplace_link import OrganizationMarketplaceLink
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.service_request import ServiceRequest
from app.models.service_property import ServiceProperty
from app.models.service_order_tracking import ServiceOrderTracking  # noqa: F401 - registers ServiceOrder relationship
from app.models.user import User
from app.models.user_commercial_profile import UserCommercialProfile
from app.routes.organization_routes import available_organizations
from app.routes.commercial_routes import MockPlanChangeRequest, UpgradeIntentRequest, commercial_mock_plan, commercial_upgrade_intent
from app.routes.payment_routes import PublicStripeCheckoutRequest, create_public_visit_checkout
from app.auth.jwt_handler import require_root_user
from app.services.commercial_upgrade_service import (
    activate_upgrade_intent,
    create_or_reuse_upgrade_intent,
    create_upgrade_intent,
    commercial_subscription_diagnostic,
    get_active_upgrade_intent,
    global_commercial_metrics,
    list_global_upgrade_intents,
    mark_payment_confirmed,
    normalize_existing_upgrade_intents,
    activate_upgrade_from_paid_payment,
)
from app.services.payment_service import handle_stripe_event, mark_payment_paid, record_cash_payment
from app.services.notification_service import notification_push_payload
from app.services.entitlement_service import account_snapshot, can_use_feature, current_plan, get_plan_limits, plan_catalog, resolve_plan
from app.services.platform_admin_service import (
    get_platform_organization,
    get_platform_user,
    list_platform_organizations,
    list_platform_users,
    platform_directory_metrics,
)


@pytest.fixture()
def commercial_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[Organization.__table__, User.__table__, Lead.__table__, OrganizationMarketplaceLink.__table__, ServiceProperty.__table__, ServiceRequest.__table__, ServiceOrder.__table__, ServiceOpportunity.__table__, CommercialSubscription.__table__, PlanChangeEvent.__table__, CommercialUpgradeIntent.__table__, Payment.__table__, PlatformLedgerEntry.__table__, AuthAuditEvent.__table__, Notification.__table__, NotificationPreference.__table__, WebPushSubscription.__table__])
    db = sessionmaker(bind=engine)()
    db.execute(text("CREATE UNIQUE INDEX uq_commercial_active_intent_org ON commercial_upgrade_intents (organization_id) WHERE status IN ('CHECKOUT_OPENED', 'PAYMENT_PENDING', 'PAYMENT_CONFIRMED', 'PAID')"))
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


def test_new_organization_and_user_start_in_free(commercial_db):
    db, actor, _, _ = commercial_db
    organization = db.query(Organization).filter_by(id=actor.organization_id).one()
    assert organization.plan == "FREE"
    assert actor.plan == "FREE"
    assert account_snapshot(db, actor)["plan"] == "FREE"
    assert account_snapshot(db, actor)["reference_price"] == "MX$ 0 / mês"
    assert not can_use_feature(db, actor, "PABLO_FULL")


def test_legacy_free_fields_cannot_override_existing_subscription(commercial_db):
    db, actor, _, _ = commercial_db
    actor.plan = "PRO"
    organization = db.query(Organization).filter_by(id=actor.organization_id).one()
    organization.plan = "STARTER"
    db.commit()
    assert account_snapshot(db, actor)["plan"] == "FREE"
    commercial_mock_plan(MockPlanChangeRequest(plan="PRO"), db, actor)
    actor.plan = "FREE"
    organization.plan = "FREE"
    db.commit()
    assert account_snapshot(db, actor)["plan"] == "PRO"


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
        "payment_id": db.query(Payment).filter(Payment.upgrade_intent_id == intent.id).one().id,
        "payment_status": "CHECKOUT_CREATED",
        "status": "CHECKOUT_OPENED",
        "reused": False,
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


def test_plan_payment_is_linked_to_intent_and_does_not_activate(commercial_db):
    db, actor, _, _ = commercial_db
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="PRO"), None, db, actor)
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    assert payment.payment_type == "SUBSCRIPTION_PLAN"
    assert payment.upgrade_intent_id == result["intent_id"]
    assert payment.status == "CHECKOUT_CREATED"
    assert db.query(CommercialSubscription).filter_by(organization_id=actor.organization_id).count() == 0


def test_duplicate_paid_stripe_event_activates_plan_once(commercial_db):
    db, actor, _, _ = commercial_db
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="BUSINESS"), None, db, actor)
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_1", "client_reference_id": str(payment.id), "payment_status": "paid", "payment_intent": "pi_test_1"}}}
    handle_stripe_event(db, event)
    db.commit()
    activate_upgrade_from_paid_payment(db, payment)
    handle_stripe_event(db, event)
    db.commit()
    assert payment.status == "PAID"
    assert db.query(CommercialSubscription).filter_by(organization_id=actor.organization_id, plan="BUSINESS").count() == 1
    assert db.query(PlanChangeEvent).filter_by(organization_id=actor.organization_id).count() == 1


def test_paid_plan_creates_one_root_financial_notification(commercial_db):
    db, actor, broker, _ = commercial_db
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="BUSINESS"), None, db, actor)
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    event = {"type": "payment_intent.succeeded", "data": {"object": {"id": "pi_plan_1", "client_reference_id": str(payment.id)}}}

    handle_stripe_event(db, event)
    activate_upgrade_from_paid_payment(db, payment)

    notifications = db.query(Notification).filter(Notification.type == "plan_payment_confirmed").all()
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.recipient_user_id == actor.id
    assert notification.organization_id == actor.organization_id
    assert notification.priority == "HIGH"
    assert "BUSINESS" in notification.message
    metadata = json.loads(notification.metadata_json)
    assert metadata["payment_id"] == payment.id
    assert metadata["commercial_upgrade_intent_id"] == result["intent_id"]
    assert broker.id not in {item.recipient_user_id for item in notifications}

    # A repeated activation/webhook is idempotent at the notification layer.
    activate_upgrade_from_paid_payment(db, payment)
    assert db.query(Notification).filter(Notification.type == "plan_payment_confirmed").count() == 1


def test_plan_payment_notification_is_tenant_scoped_and_push_payload_is_sanitized(commercial_db):
    db, actor, _, other = commercial_db
    other_root = User(username="other-root", full_name="Other Root", password_hash="hash", role="ROOT", organization_id=other.organization_id, status="ACTIVE", is_active=True)
    db.add(other_root)
    db.commit()
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="PRO"), None, db, actor)
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    handle_stripe_event(db, {"type": "payment_intent.succeeded", "data": {"object": {"id": "pi_plan_2", "client_reference_id": str(payment.id)}}})
    activate_upgrade_from_paid_payment(db, payment)

    notification = db.query(Notification).filter(Notification.type == "plan_payment_confirmed").one()
    payload = notification_push_payload(db, notification)
    assert notification.recipient_user_id == actor.id
    assert other_root.id != notification.recipient_user_id
    assert payload["url"].endswith("/?section=plans&financial=payments")
    assert "sk_test" not in json.dumps(payload)
    assert "whsec" not in json.dumps(payload)


def test_financial_push_preference_off_keeps_in_app_event_without_push(commercial_db, monkeypatch):
    from app.services import notification_service

    db, actor, _, _ = commercial_db
    calls = []
    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("WEB_PUSH_CONTACT_EMAIL", "root@totalsolutions.test")
    monkeypatch.setattr(notification_service, "webpush", lambda **kwargs: calls.append(kwargs))
    preferences = NotificationPreference(user_id=actor.id, organization_id=actor.organization_id, browser_enabled=True, financial_plan_sales=False)
    db.add(preferences)
    db.add(WebPushSubscription(
        organization_id=actor.organization_id,
        user_id=actor.id,
        endpoint="https://push.example.test/financial-off",
        p256dh="client-public-key",
        auth="client-auth-secret",
        active=True,
    ))
    db.commit()
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="PRO"), None, db, actor)
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    handle_stripe_event(db, {"type": "payment_intent.succeeded", "data": {"object": {"id": "pi_plan_3", "client_reference_id": str(payment.id)}}})
    activate_upgrade_from_paid_payment(db, payment)

    assert db.query(Notification).filter(Notification.type == "plan_payment_confirmed").count() == 1
    assert calls == []


def test_financial_push_can_aggregate_without_removing_individual_event(commercial_db, monkeypatch):
    from app.services import notification_service

    db, actor, _, _ = commercial_db
    calls = []
    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("WEB_PUSH_CONTACT_EMAIL", "root@totalsolutions.test")
    monkeypatch.setenv("FINANCIAL_PLAN_SALES_AGGREGATION_THRESHOLD", "1")
    monkeypatch.setattr(notification_service, "webpush", lambda **kwargs: calls.append(kwargs))
    db.add(NotificationPreference(user_id=actor.id, organization_id=actor.organization_id, browser_enabled=True))
    db.add(WebPushSubscription(
        organization_id=actor.organization_id,
        user_id=actor.id,
        endpoint="https://push.example.test/financial-aggregate",
        p256dh="client-public-key",
        auth="client-auth-secret",
        active=True,
    ))
    db.commit()
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="PRO"), None, db, actor)
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    handle_stripe_event(db, {"type": "payment_intent.succeeded", "data": {"object": {"id": "pi_plan_4", "client_reference_id": str(payment.id)}}})
    activate_upgrade_from_paid_payment(db, payment)

    assert db.query(Notification).filter(Notification.type == "plan_payment_confirmed").count() == 1
    assert len(calls) == 1
    assert "planos vendidos" in json.loads(calls[0]["data"])["title"]


def test_unpaid_checkout_event_does_not_activate_plan(commercial_db):
    db, actor, _, _ = commercial_db
    result = commercial_upgrade_intent(UpgradeIntentRequest(plan="PRO"), None, db, actor)
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_unpaid", "client_reference_id": str(payment.id), "payment_status": "unpaid"}}}
    handle_stripe_event(db, event)
    db.commit()
    assert payment.status == "CHECKOUT_CREATED"
    assert db.query(CommercialSubscription).filter_by(organization_id=actor.organization_id).count() == 0
    assert db.query(Notification).filter(Notification.type == "plan_payment_confirmed").count() == 0


def test_public_visit_checkout_uses_order_amount_and_is_idempotent(commercial_db, monkeypatch):
    db, actor, _, _ = commercial_db
    lead = Lead(organization_id=actor.organization_id, nome="Cliente", tipo_servico="HIDRAULICA")
    db.add(lead)
    db.flush()
    request = ServiceRequest(
        organization_id=actor.organization_id, lead_id=lead.id, tracking_token="public-payment-token",
        service_category="HIDRAULICA", requester_name="Cliente",
    )
    db.add(request)
    db.flush()
    order = ServiceOrder(
        organization_id=actor.organization_id, lead_id=lead.id, service_request_id=request.id,
        visit_calculated_price=Decimal("450.00"), status="ABERTA",
    )
    db.add(order)
    db.commit()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sandbox-test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setattr(
        "app.services.payment_service._stripe_request",
        lambda *args, **kwargs: {"id": "cs_visit_1", "url": "https://checkout.stripe.test/cs_visit_1"},
    )

    result = create_public_visit_checkout(
        "public-payment-token", PublicStripeCheckoutRequest(), "visit-key-1", db,
    )
    repeated = create_public_visit_checkout(
        "public-payment-token", PublicStripeCheckoutRequest(), "visit-key-1", db,
    )
    payment = db.query(Payment).filter(Payment.id == result["payment_id"]).one()
    assert result == repeated
    assert payment.payment_type == "TECHNICAL_VISIT"
    assert payment.gross_amount == Decimal("450.00")
    assert payment.status == "CHECKOUT_CREATED"
    assert payment.paid_at is None
    assert db.query(Payment).filter(Payment.service_order_id == order.id).count() == 1


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
    assert confirmed.status == "PAYMENT_CONFIRMED"
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


def test_second_click_reuses_same_intent(commercial_db):
    db, actor, _, _ = commercial_db
    first, reused_first = create_or_reuse_upgrade_intent(db, actor, "PRO")
    second, reused_second = create_or_reuse_upgrade_intent(db, actor, "PRO")
    assert reused_first is False
    assert reused_second is True
    assert first.id == second.id
    assert db.query(CommercialUpgradeIntent).filter_by(organization_id=actor.organization_id).count() == 1
    assert db.query(AuthAuditEvent).filter_by(event_type="UPGRADE_INTENT_REUSED").count() == 1


def test_database_constraint_blocks_two_active_intents_for_one_organization(commercial_db):
    db, actor, _, _ = commercial_db
    create_upgrade_intent(db, actor, "PRO")
    db.add(CommercialUpgradeIntent(organization_id=actor.organization_id, user_id=actor.id, requested_plan="BUSINESS", reference_price_mxn=1999, provider="CLIP", status="CHECKOUT_OPENED"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_pro_pending_is_replaced_by_business_and_history_is_preserved(commercial_db):
    db, actor, _, _ = commercial_db
    pro = create_upgrade_intent(db, actor, "PRO")
    business, reused = create_or_reuse_upgrade_intent(db, actor, "BUSINESS")
    db.refresh(pro)
    assert reused is False
    assert pro.status == "CANCELLED"
    assert business.requested_plan == "BUSINESS"
    assert get_active_upgrade_intent(db, actor).id == business.id
    assert db.query(CommercialUpgradeIntent).filter_by(organization_id=actor.organization_id).count() == 2
    assert db.query(AuthAuditEvent).filter_by(event_type="UPGRADE_PLAN_CHANGED").count() == 1


def test_confirmed_payment_blocks_new_checkout(commercial_db):
    db, actor, _, _ = commercial_db
    original = create_upgrade_intent(db, actor, "PRO")
    mark_payment_confirmed(db, actor, original.id)
    same, reused_same = create_or_reuse_upgrade_intent(db, actor, "PRO")
    different, reused_different = create_or_reuse_upgrade_intent(db, actor, "BUSINESS")
    assert same.id == original.id and reused_same is True
    assert different.id == original.id and reused_different is True
    assert db.query(CommercialUpgradeIntent).filter_by(organization_id=actor.organization_id).count() == 1


def test_activation_removes_intent_from_active_queue(commercial_db):
    db, actor, _, _ = commercial_db
    intent = create_upgrade_intent(db, actor, "PRO")
    mark_payment_confirmed(db, actor, intent.id)
    activate_upgrade_intent(db, actor, intent.id)
    assert get_active_upgrade_intent(db, actor) is None


def test_current_plan_upgrade_rules(commercial_db):
    db, actor, _, _ = commercial_db
    commercial_mock_plan(MockPlanChangeRequest(plan="PRO"), db, actor)
    with pytest.raises(Exception) as same_plan:
        create_upgrade_intent(db, actor, "PRO")
    assert getattr(same_plan.value, "status_code", None) == 409
    business, _ = create_or_reuse_upgrade_intent(db, actor, "BUSINESS")
    assert business.requested_plan == "BUSINESS"
    mark_payment_confirmed(db, actor, business.id)
    activate_upgrade_intent(db, actor, business.id)
    with pytest.raises(Exception) as business_again:
        create_upgrade_intent(db, actor, "BUSINESS")
    with pytest.raises(Exception) as downgrade:
        create_upgrade_intent(db, actor, "PRO")
    assert getattr(business_again.value, "status_code", None) == 409
    assert getattr(downgrade.value, "status_code", None) == 409


def test_legacy_duplicate_intents_are_normalized_without_deleting_history(commercial_db):
    db, actor, _, _ = commercial_db
    db.execute(text("DROP INDEX uq_commercial_active_intent_org"))
    first = create_upgrade_intent(db, actor, "PRO")
    first.status = "CANCELLED"
    db.commit()


def test_root_global_commercial_queue_and_metrics_keep_other_org_free_until_activation(commercial_db):
    db, actor, _, other = commercial_db
    intent = create_upgrade_intent(db, other, "PRO")
    metrics = global_commercial_metrics(db)
    assert metrics["checkouts_opened"] == 1
    assert metrics["potential_pending_mxn"] == 499.0
    assert metrics["active_mrr_reference_mxn"] == 0
    rows = list_global_upgrade_intents(db)
    row = next(item for item in rows if item["id"] == intent.id)
    assert row["organization_id"] == other.organization_id
    assert row["current_plan"] == "FREE"
    assert row["requested_plan"] == "PRO"

    confirmed = mark_payment_confirmed(db, actor, intent.id, global_scope=True)
    assert confirmed.confirmation_source == "MANUAL_ADMIN"
    assert account_snapshot(db, other)["plan"] == "FREE"
    assert global_commercial_metrics(db)["payments_confirmed_awaiting_activation"] == 1

    activated, subscription = activate_upgrade_intent(db, actor, intent.id, global_scope=True)
    assert activated.status == "ACTIVATED"
    assert subscription.organization_id == other.organization_id
    assert account_snapshot(db, other)["plan"] == "PRO"
    assert account_snapshot(db, actor)["plan"] == "FREE"
    final_metrics = global_commercial_metrics(db)
    assert final_metrics["activations_completed"] == 1
    assert final_metrics["active_mrr_reference_mxn"] == 499


def test_non_root_cannot_confirm_global_commercial_intent(commercial_db):
    db, actor, broker, other = commercial_db
    intent = create_upgrade_intent(db, other, "BUSINESS")
    with pytest.raises(Exception) as error:
        mark_payment_confirmed(db, broker, intent.id, global_scope=True)
    assert getattr(error.value, "status_code", None) == 403
    assert intent.status == "CHECKOUT_OPENED"


def test_root_platform_directory_finds_other_organization_user_without_mutation(commercial_db):
    db, actor, broker, other = commercial_db
    other.email = "magnoalvesbrasil@proton.me"
    other.telefone = "+52 998 555 7821"
    other.manager_id = None
    intent = create_upgrade_intent(db, other, "PRO")
    db.commit()
    organization_id = other.organization_id
    manager_id = other.manager_id
    intent_status = intent.status

    rows = list_platform_users(db, search="magnoalvesbrasil@proton.me")
    assert len(rows) == 1
    assert rows[0]["organization_id"] == organization_id
    assert rows[0]["email"] == "magnoalvesbrasil@proton.me"
    assert rows[0]["telefone"].endswith("7821")
    assert db.query(CommercialUpgradeIntent).filter_by(id=intent.id).one().status == intent_status
    assert other.organization_id == organization_id
    assert other.manager_id == manager_id


def test_platform_directory_uses_organization_subscription_and_supports_details(commercial_db):
    db, actor, broker, other = commercial_db
    subscription = CommercialSubscription(organization_id=other.organization_id, plan="PRO", status="ACTIVE", provider="CLIP")
    db.add(subscription)
    db.commit()

    organizations = list_platform_organizations(db, search=str(other.organization_id))
    assert len(organizations) == 1
    assert organizations[0]["plan"] == "PRO"
    assert organizations[0]["subscription_status"] == "ACTIVE"
    detail = get_platform_organization(db, other.organization_id)
    assert detail["plan"] == "PRO"
    assert any(user["id"] == other.id for user in detail["users"])
    user_detail = get_platform_user(db, other.id)
    assert user_detail["plan"] == "PRO"


def test_platform_metrics_are_backend_calculated_and_global(commercial_db):
    db, actor, broker, other = commercial_db
    db.add(CommercialSubscription(organization_id=other.organization_id, plan="BUSINESS", status="ACTIVE", provider="CLIP"))
    db.commit()
    metrics = platform_directory_metrics(db)
    assert metrics["organizations_total"] == 2
    assert metrics["organizations_free"] == 1
    assert metrics["organizations_business"] == 1
    assert metrics["users_total"] == 3
    assert metrics["technicians_total"] == 2


def test_platform_directory_guard_rejects_non_root_roles(commercial_db):
    db, actor, broker, other = commercial_db
    with pytest.raises(Exception) as error:
        require_root_user(broker)
    assert getattr(error.value, "status_code", None) == 403


@pytest.mark.parametrize("subscription_plan", ["PRO", "BUSINESS"])
def test_available_organizations_uses_subscription_plan_over_legacy_organization_plan(commercial_db, subscription_plan):
    db, actor, _, _ = commercial_db
    organization = db.query(Organization).filter_by(id=actor.organization_id).one()
    organization.plan = "FREE"
    db.add(CommercialSubscription(
        organization_id=organization.id,
        plan=subscription_plan,
        status="ACTIVE",
        provider="CLIP",
    ))
    db.commit()

    rows = available_organizations(db, actor)

    row = next(item for item in rows if item["id"] == organization.id)
    assert row["plan"] == subscription_plan


def test_activation_updates_canonical_subscription_and_is_idempotent(commercial_db):
    db, actor, _, _ = commercial_db
    organization = db.query(Organization).filter_by(id=actor.organization_id).one()
    subscription = CommercialSubscription(
        organization_id=organization.id,
        plan="FREE",
        status="LAUNCH_ACCESS",
        provider="MOCK",
    )
    db.add(subscription)
    db.commit()

    intent = create_upgrade_intent(db, actor, "PRO")
    mark_payment_confirmed(db, actor, intent.id)
    activated, first_subscription = activate_upgrade_intent(db, actor, intent.id)
    activated_again, second_subscription = activate_upgrade_intent(db, actor, intent.id)

    assert activated.status == "ACTIVATED"
    assert activated_again.id == intent.id
    assert first_subscription.id == second_subscription.id == subscription.id
    assert subscription.plan == organization.plan == "PRO"
    assert current_plan(db, actor) == "PRO"
    assert db.query(CommercialSubscription).filter_by(organization_id=organization.id).count() == 1
    assert db.query(PlanChangeEvent).filter_by(organization_id=organization.id).count() == 1


def test_commercial_subscription_diagnostic_reports_plan_sources_and_history(commercial_db):
    db, actor, _, _ = commercial_db
    organization = db.query(Organization).filter_by(id=actor.organization_id).one()
    db.add(CommercialSubscription(
        organization_id=organization.id,
        plan="FREE",
        status="LAUNCH_ACCESS",
        provider="MOCK",
    ))
    db.commit()
    intent = create_upgrade_intent(db, actor, "PRO")
    mark_payment_confirmed(db, actor, intent.id)
    activate_upgrade_intent(db, actor, intent.id)

    diagnostic = commercial_subscription_diagnostic(db, organization.id)

    assert diagnostic["organization"]["plan"] == "PRO"
    assert diagnostic["subscription_count"] == 1
    assert diagnostic["subscription"]["plan"] == "PRO"
    assert diagnostic["subscription"]["status"] == "LAUNCH_ACCESS"
    assert diagnostic["resolved"]["current_plan"] == "PRO"
    assert diagnostic["resolved"]["resolve_plan"]["source"] == "ORGANIZATION_SUBSCRIPTION"
    assert diagnostic["latest_intents"][0]["from_plan"] == "FREE"
    assert diagnostic["latest_intents"][0]["target_plan"] == "PRO"


def test_active_organization_subscription_precedes_free_user_profile(commercial_db):
    db, actor, _, _ = commercial_db
    db.add(CommercialSubscription(
        organization_id=actor.organization_id,
        plan="PRO",
        status="LAUNCH_ACCESS",
        provider="MOCK",
    ))
    UserCommercialProfile.__table__.create(bind=db.get_bind(), checkfirst=True)
    db.add(UserCommercialProfile(user_id=actor.id, plan="FREE", status="ACTIVE", source="ONBOARDING"))
    db.commit()

    resolved = resolve_plan(db, actor)

    assert resolved == {"plan": "PRO", "source": "ORGANIZATION_SUBSCRIPTION"}
    assert current_plan(db, actor) == "PRO"
    assert get_plan_limits(db, actor)["users"] == 5


@pytest.mark.parametrize("organization_plan", ["PRO", "BUSINESS"])
def test_broker_account_uses_organization_subscription_not_individual_profile(commercial_db, organization_plan):
    db, _, broker, _ = commercial_db
    db.add(CommercialSubscription(
        organization_id=broker.organization_id,
        plan=organization_plan,
        status="ACTIVE",
        provider="STRIPE",
    ))
    UserCommercialProfile.__table__.create(bind=db.get_bind(), checkfirst=True)
    db.add(UserCommercialProfile(user_id=broker.id, plan="FREE", status="ACTIVE", source="ONBOARDING"))
    db.commit()

    assert account_snapshot(db, broker)["plan"] == organization_plan
    assert account_snapshot(db, broker)["organization_plan"] == organization_plan
    # Individual entitlement resolution remains intentionally unchanged.
    assert resolve_plan(db, broker) == {"plan": "FREE", "source": "USER_COMMERCIAL_PROFILE"}


def test_broker_account_without_subscription_keeps_safe_free_fallback(commercial_db):
    db, _, broker, _ = commercial_db
    UserCommercialProfile.__table__.create(bind=db.get_bind(), checkfirst=True)
    db.add(UserCommercialProfile(user_id=broker.id, plan="FREE", status="ACTIVE", source="ONBOARDING"))
    db.commit()

    snapshot = account_snapshot(db, broker)

    assert snapshot["plan"] == "FREE"
    assert snapshot["organization_plan"] == "FREE"


def test_manager_account_uses_organization_subscription(commercial_db):
    db, _, broker, _ = commercial_db
    broker.role = "GERENTE"
    db.add(CommercialSubscription(
        organization_id=broker.organization_id,
        plan="BUSINESS",
        status="ACTIVE",
        provider="STRIPE",
    ))
    db.commit()

    assert account_snapshot(db, broker)["plan"] == "BUSINESS"


def test_manager_cannot_confirm_or_activate_commercial_intent(commercial_db):
    db, actor, _, _ = commercial_db
    intent = create_upgrade_intent(db, actor, "PRO")
    actor.role = "GERENTE"
    db.commit()

    with pytest.raises(Exception) as confirm_error:
        mark_payment_confirmed(db, actor, intent.id)
    with pytest.raises(Exception) as activate_error:
        activate_upgrade_intent(db, actor, intent.id)

    assert getattr(confirm_error.value, "status_code", None) == 403
    assert getattr(activate_error.value, "status_code", None) == 403
    assert intent.status == "CHECKOUT_OPENED"


def test_commercial_admin_controls_are_root_only_in_frontend():
    frontend = Path(__file__).parents[2].joinpath("frontend/index.html").read_text()
    intent_render = frontend.split("async function loadCommercialUpgradeIntents", 1)[1].split("async function loadCommercialAdminMetrics", 1)[0]
    assert "${isRoot ? `<div class=\"inline-actions\">" in intent_render
    assert "data-commercial-intent-action=\"activate\"" in intent_render
