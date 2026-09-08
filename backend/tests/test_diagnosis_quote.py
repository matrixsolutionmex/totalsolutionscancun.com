from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models import organization, service_order, service_order_tracking, service_request, service_property, organization_marketplace_link, lead, user, service_order_diagnosis, service_order_quote, service_order_payment_plan, commercial_upgrade_intent
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
from app.services.service_order_payment_plan_service import payment_plan_projection, create_payment_plan_for_approved_quote, record_service_installment_payment, create_installment_checkout, release_installment_for_payment, PUBLIC_INSTALLMENT_TYPES
from app.services.payment_service import reconcile_stripe_checkout_payment
from app.services.service_order_financial_service import ensure_financial_account, create_visit_pricing_snapshot, record_visit_payment
from app.models.service_order_financial import ServiceOrderFinancial
from app.models.payment import Payment
from app.models.service_order_ledger_entry import ServiceOrderLedgerEntry
from app.models.service_order_installment_release_event import ServiceOrderInstallmentReleaseEvent


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
    plan = payment_plan_projection(db, order)
    assert plan["approved_total"] == Decimal("450.00")
    assert "id" not in plan
    assert len(plan["installments"]) == 1
    assert plan["installments"][0]["status"] == "AVAILABLE"
    assert plan["installments"][0]["type"] == "FULL"
    assert plan["installments"][0]["can_release"] is False
    assert "SERVICE_FULL" not in str(plan)
    assert "id" not in plan["installments"][0]
    assert db.query(Payment).count() == 0


def test_approved_quote_payment_plan_uses_policy_snapshot_and_is_idempotent(db):
    _, _, manager, _, _, _, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": 10000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    db.flush()
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    same_plan = create_payment_plan_for_approved_quote(db, order, quote)
    assert same_plan.id == plan.id
    projection = payment_plan_projection(db, order)
    assert [item["amount"] for item in projection["installments"]] == [Decimal("3000.00"), Decimal("7000.00")]
    assert projection["installments"][0]["status"] == "AVAILABLE"
    assert projection["installments"][1]["status"] == "PENDING"
    assert [item["can_release"] for item in projection["installments"]] == [False, False]
    assert projection["service_outstanding_balance"] == Decimal("10000.00")
    assert len(db.query(service_order_payment_plan.ServiceOrderPaymentPlan).all()) == 1


def test_public_payment_plan_omits_administrative_release_capability(db):
    _, _, manager, _, _, _, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Project", "quantity": 1, "unit_price": 30000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    db.flush()
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    plan.installments[0].status = "PAID"
    db.flush()

    public_projection = payment_plan_projection(db, order, include_release_capability=False)
    internal_projection = payment_plan_projection(db, order)

    assert "can_release" not in public_projection["installments"][1]
    assert internal_projection["installments"][1]["can_release"] is True
    assert plan.id == db.query(service_order_payment_plan.ServiceOrderPaymentPlan).one().id


def test_large_quote_plan_is_rounded_without_losing_total(db):
    _, _, manager, _, _, _, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Project", "quantity": 1, "unit_price": 30000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    db.flush()
    create_payment_plan_for_approved_quote(db, order, quote)
    projection = payment_plan_projection(db, order)
    assert [item["amount"] for item in projection["installments"]] == [Decimal("9000.00"), Decimal("12000.00"), Decimal("9000.00")]
    assert sum((Decimal(str(item["amount"])) for item in projection["installments"]), Decimal("0")) == Decimal("30000.00")


def test_service_installment_payment_is_idempotent_and_does_not_change_operation(db):
    _, _, manager, _, tech, _, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": 1000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    db.flush()
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    installment = plan.installments[0]
    payment = Payment(organization_id=order.organization_id, service_order_id=order.id, technician_id=tech.id, installment_id=installment.id, payment_type="SERVICE_FULL", payment_method="STRIPE_CARD", currency="MXN", gross_amount=Decimal("1000"), provider="STRIPE", idempotency_key="service-test-1", status="PENDING")
    db.add(payment)
    db.flush()
    payload = {"amount_received": 100000, "currency": "mxn", "payment_intent": "pi_test"}
    record_service_installment_payment(db, payment, provider_payload=payload)
    record_service_installment_payment(db, payment, provider_payload=payload)
    db.commit()
    assert payment.status == "PAID"
    assert installment.status == "PAID"
    assert db.query(ServiceOrderLedgerEntry).filter_by(service_order_id=order.id, entry_type="SERVICE_PAYMENT").count() == 1
    assert order.status == "ABERTA"


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


def test_new_quote_version_supersedes_unpaid_approved_quote_and_plan(db):
    _, _, manager, _, _, _, order = scenario(db)
    first = create_quote(db, order, manager, {"items": [{"description": "Service v1", "quantity": 1, "unit_price": 2800}]})
    first.status = "SENT"
    db.flush()
    approve_public_quote(db, order)
    db.commit()

    second = create_quote(db, order, manager, {"items": [{"description": "Service v2", "quantity": 1, "unit_price": 2800}]})
    assert first.status == "SUPERSEDED"
    old_plan = db.query(service_order_payment_plan.ServiceOrderPaymentPlan).filter_by(quote_id=first.id).one()
    assert old_plan.status == "SUPERSEDED"
    second.status = "SENT"
    db.flush()
    approve_public_quote(db, order)
    db.commit()

    plans = db.query(service_order_payment_plan.ServiceOrderPaymentPlan).filter_by(service_order_id=order.id).all()
    assert len(plans) == 2
    assert sum(plan.status == "ACTIVE" for plan in plans) == 1
    assert second.status == "APPROVED"


def test_paid_approved_quote_cannot_be_replaced_silently(db):
    _, _, manager, _, tech, _, order = scenario(db)
    first = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": 2800}]})
    first.status = "APPROVED"
    first.approved_total = first.total
    db.flush()
    plan = create_payment_plan_for_approved_quote(db, order, first)
    installment = plan.installments[0]
    payment = Payment(organization_id=order.organization_id, service_order_id=order.id, technician_id=tech.id, installment_id=installment.id, payment_type="SERVICE_FULL", payment_method="STRIPE_CARD", currency="MXN", gross_amount=Decimal("2800"), provider="STRIPE", idempotency_key="service-paid-before-revision", status="PAID")
    db.add(payment)
    installment.status = "PAID"
    db.flush()
    with pytest.raises(HTTPException) as exc:
        create_quote(db, order, manager, {"items": [{"description": "Replacement", "quantity": 1, "unit_price": 2800}]})
    assert exc.value.status_code == 409


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


def test_public_payment_plan_uses_localized_labels_and_financial_summary():
    html = (Path(__file__).parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    for phrase in (
        "Pago total", "Anticipo", "Pago intermedio", "Pago final", "Pagado", "Saldo restante",
        "Full payment", "Deposit", "Progress payment", "Final payment", "Paid", "Remaining balance",
        "Pagamento integral", "Entrada", "Parcela intermediária", "Parcela final", "Pago", "Total do serviço",
        "publicInstallmentType", "serviceTotal", "service_paid_total", "service_outstanding_balance",
    ):
        assert phrase in html
    assert 'SERVICE_STAGE_1: "paymentTypeDeposit"' in html
    assert 'SERVICE_STAGE_2: "paymentTypeProgress"' in html
    assert 'SERVICE_STAGE_3: "paymentTypeFinal"' in html
    assert PUBLIC_INSTALLMENT_TYPES["SERVICE_STAGE_1"] == "DEPOSIT"
    assert PUBLIC_INSTALLMENT_TYPES["SERVICE_STAGE_2"] == "PROGRESS"
    assert PUBLIC_INSTALLMENT_TYPES["SERVICE_STAGE_3"] == "FINAL"
    assert '${escapeHtml(item.type)}' not in html
    assert 'trackingText("subtotal"))}: ${escapeHtml(formatVisitAmount(plan.service_outstanding_balance' not in html


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


@pytest.mark.parametrize(("amount", "expected"), [
    ("2800", [("SERVICE_FULL", "100.00", "2800.00", "AVAILABLE")]),
    ("3000", [("SERVICE_FULL", "100.00", "3000.00", "AVAILABLE")]),
    ("3000.01", [("SERVICE_DEPOSIT", "30.00", "900.00", "AVAILABLE"), ("SERVICE_COMPLETION", "70.00", "2100.01", "PENDING")]),
    ("10000", [("SERVICE_DEPOSIT", "30.00", "3000.00", "AVAILABLE"), ("SERVICE_COMPLETION", "70.00", "7000.00", "PENDING")]),
    ("15000", [("SERVICE_DEPOSIT", "30.00", "4500.00", "AVAILABLE"), ("SERVICE_COMPLETION", "70.00", "10500.00", "PENDING")]),
    ("15000.01", [("SERVICE_STAGE_1", "30.00", "4500.00", "AVAILABLE"), ("SERVICE_STAGE_2", "40.00", "6000.00", "PENDING"), ("SERVICE_STAGE_3", "30.00", "4500.01", "PENDING")]),
    ("30000", [("SERVICE_STAGE_1", "30.00", "9000.00", "AVAILABLE"), ("SERVICE_STAGE_2", "40.00", "12000.00", "PENDING"), ("SERVICE_STAGE_3", "30.00", "9000.00", "PENDING")]),
])
def test_payment_plan_boundaries_and_schedule_are_exact(db, amount, expected):
    _, _, manager, _, _, _, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": Decimal(amount)}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    actual = [(item.installment_type, f"{item.percentage:.2f}", f"{item.amount:.2f}", item.status) for item in plan.installments]
    assert actual == expected
    assert sum((item.amount for item in plan.installments), Decimal("0.00")) == Decimal(amount).quantize(Decimal("0.01"))


@pytest.mark.parametrize("amount", ["3333.33", "10001.01", "15001.01", "33333.33"])
def test_payment_plan_rounding_preserves_every_cent(db, amount):
    _, _, manager, _, _, _, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": Decimal(amount)}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    assert sum((item.amount for item in plan.installments), Decimal("0.00")) == Decimal(amount).quantize(Decimal("0.01"))


def test_policy_snapshot_does_not_change_existing_plan(db):
    _, _, manager, _, _, _, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": 10000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    policy = plan.policy_snapshot.copy()
    from app.models.organization_payment_policy import OrganizationPaymentPolicy
    db.query(OrganizationPaymentPolicy).filter_by(organization_id=order.organization_id).update({"medium_deposit_percentage": Decimal("50")})
    db.flush()
    assert plan.policy_snapshot == policy
    assert [item.amount for item in plan.installments] == [Decimal("3000.00"), Decimal("7000.00")]
    second = create_quote(db, order, manager, {"items": [{"description": "Service v2", "quantity": 1, "unit_price": 10000}]})
    second.status = "APPROVED"
    second.approved_total = second.total
    new_plan = create_payment_plan_for_approved_quote(db, order, second)
    assert [item.amount for item in new_plan.installments] == [Decimal("5000.00"), Decimal("5000.00")]


def test_duplicate_installment_checkout_reuses_pending_payment_and_never_accepts_client_amount(db, monkeypatch):
    _, _, manager, _, _, request, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": 10000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    calls = []

    def fake_checkout(db_session, payment, **kwargs):
        calls.append(kwargs)
        payment.stripe_checkout_session_id = "cs_test_fake"
        payment.checkout_url = "https://checkout.test/fake"
        return payment

    monkeypatch.setattr("app.services.payment_service.create_stripe_checkout", fake_checkout)
    first = create_installment_checkout(db, order, 1, tracking_token=request.tracking_token)
    second = create_installment_checkout(db, order, 1, tracking_token=request.tracking_token)
    assert first.id == second.id
    assert len(calls) == 1
    assert first.gross_amount == Decimal("3000.00")
    assert db.query(Payment).filter_by(service_order_id=order.id).count() == 1


def test_pending_installment_cross_tenant_and_old_quote_are_blocked(db):
    one, two, manager, _, _, request, order = scenario(db)
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": 10000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    with pytest.raises(HTTPException) as exc:
        create_installment_checkout(db, order, 2, tracking_token=request.tracking_token)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        create_installment_checkout(db, order, 99, tracking_token=request.tracking_token)
    assert exc.value.status_code == 404
    latest = create_quote(db, order, manager, {"items": [{"description": "Replacement", "quantity": 1, "unit_price": 11000}]})
    db.flush()
    with pytest.raises(HTTPException) as exc:
        create_installment_checkout(db, order, 1, tracking_token=request.tracking_token)
    assert exc.value.status_code == 409
    assert one.id != two.id


def _approved_plan(db, manager, order, amount):
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": Decimal(amount)}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    db.flush()
    return quote, create_payment_plan_for_approved_quote(db, order, quote)


def test_future_final_release_requires_paid_deposit_and_is_idempotent(db):
    _, _, manager, _, _, _, order = scenario(db)
    _, plan = _approved_plan(db, manager, order, "10000")
    final = plan.installments[1]
    with pytest.raises(HTTPException) as exc:
        release_installment_for_payment(db, order, 2, manager, trigger_type="SERVICE_READY_FOR_FINAL_PAYMENT")
    assert exc.value.status_code == 409
    plan.installments[0].status = "PAID"
    projection = payment_plan_projection(db, order)
    assert [item["can_release"] for item in projection["installments"]] == [False, True]
    first = release_installment_for_payment(db, order, 2, manager, trigger_type="SERVICE_READY_FOR_FINAL_PAYMENT", observation="Final ready")
    second = release_installment_for_payment(db, order, 2, manager, trigger_type="SERVICE_READY_FOR_FINAL_PAYMENT")
    db.commit()
    assert first == {"installment_id": final.id, "status": "AVAILABLE", "changed": True}
    assert second == {"installment_id": final.id, "status": "AVAILABLE", "changed": False}
    assert db.query(ServiceOrderInstallmentReleaseEvent).filter_by(installment_id=final.id).count() == 1
    projection = payment_plan_projection(db, order)
    assert projection["installments"][1]["status"] == "AVAILABLE"
    assert projection["installments"][1]["checkout_available"] is True
    assert projection["installments"][1]["can_release"] is False
    assert projection["installments"][0]["status"] == "PAID"


def test_large_plan_releases_progress_then_final_only_after_prior_payment(db):
    _, _, manager, _, _, _, order = scenario(db)
    _, plan = _approved_plan(db, manager, order, "30000")
    progress, final = plan.installments[1:]
    with pytest.raises(HTTPException) as exc:
        release_installment_for_payment(db, order, 2, manager, trigger_type="PROGRESS_STAGE_COMPLETED")
    assert exc.value.status_code == 409
    plan.installments[0].status = "PAID"
    projection = payment_plan_projection(db, order)
    assert [item["can_release"] for item in projection["installments"]] == [False, True, False]
    release_installment_for_payment(db, order, 2, manager, trigger_type="PROGRESS_STAGE_COMPLETED")
    with pytest.raises(HTTPException) as exc:
        release_installment_for_payment(db, order, 3, manager, trigger_type="SERVICE_READY_FOR_FINAL_PAYMENT")
    assert exc.value.status_code == 409
    progress.status = "PAID"
    result = release_installment_for_payment(db, order, 3, manager, trigger_type="SERVICE_READY_FOR_FINAL_PAYMENT")
    db.commit()
    assert result["status"] == "AVAILABLE"
    assert progress.status == "PAID"
    assert final.status == "AVAILABLE"
    projection = payment_plan_projection(db, order)
    assert [item["status"] for item in projection["installments"]] == ["PAID", "PAID", "AVAILABLE"]
    assert [item["checkout_available"] for item in projection["installments"]] == [False, False, True]
    assert [item["can_release"] for item in projection["installments"]] == [False, False, False]


def test_installment_release_rejects_invalid_trigger_and_unauthorized_tenant(db):
    one, two, manager, other, _, _, order = scenario(db)
    _, plan = _approved_plan(db, manager, order, "30000")
    plan.installments[0].status = "PAID"
    with pytest.raises(HTTPException) as exc:
        release_installment_for_payment(db, order, 2, manager, trigger_type="ARBITRARY")
    assert exc.value.status_code == 400
    other.organization_id = two.id
    with pytest.raises(HTTPException) as exc:
        release_installment_for_payment(db, order, 2, other, trigger_type="PROGRESS_STAGE_COMPLETED")
    assert exc.value.status_code == 403
    assert one.id != two.id


def test_visit_and_service_money_are_separate(db):
    _, _, manager, _, tech, _, order = scenario(db)
    account = ensure_financial_account(db, order, organization_id=order.organization_id)
    snapshot = create_visit_pricing_snapshot(db, order, organization_id=order.organization_id, pricing={"total_amount": Decimal("450"), "currency": "MXN"})
    visit = Payment(organization_id=order.organization_id, service_order_id=order.id, technician_id=tech.id, payment_type="TECHNICAL_VISIT", payment_method="STRIPE_CARD", currency="MXN", gross_amount=Decimal("450"), provider="STRIPE", idempotency_key="visit-separation", status="PENDING")
    db.add(visit)
    db.flush()
    record_visit_payment(db, visit, provider_payload={"amount_received": 45000, "currency": "mxn", "payment_intent": "pi_visit"})
    quote = create_quote(db, order, manager, {"items": [{"description": "Service", "quantity": 1, "unit_price": 10000}]})
    quote.status = "APPROVED"
    quote.approved_total = quote.total
    plan = create_payment_plan_for_approved_quote(db, order, quote)
    service_payment = Payment(organization_id=order.organization_id, service_order_id=order.id, technician_id=tech.id, installment_id=plan.installments[0].id, payment_type="SERVICE_DEPOSIT", payment_method="STRIPE_CARD", currency="MXN", gross_amount=Decimal("3000"), provider="STRIPE", idempotency_key="service-separation", status="PENDING")
    db.add(service_payment)
    db.flush()
    record_service_installment_payment(db, service_payment, provider_payload={"amount_received": 300000, "currency": "mxn", "payment_intent": "pi_service"})
    projection = payment_plan_projection(db, order)
    assert snapshot.total_amount == Decimal("450.00")
    assert account.visit_fee == Decimal("450.00")
    assert account.service_paid_amount == Decimal("3000.00")
    assert account.service_outstanding_balance == Decimal("7000.00")
    assert db.query(ServiceOrderLedgerEntry).filter_by(service_order_id=order.id, entry_type="VISIT_PAYMENT").count() == 1
    assert db.query(ServiceOrderLedgerEntry).filter_by(service_order_id=order.id, entry_type="SERVICE_PAYMENT").count() == 1
    assert projection["approved_total"] == Decimal("10000.00")
    assert projection["service_paid_total"] == Decimal("3000.00")
    assert projection["service_outstanding_balance"] == Decimal("7000.00")


def test_reconcile_paid_checkout_uses_webhook_accounting_path_and_is_idempotent(db, monkeypatch):
    _, _, manager, _, tech, _, order = scenario(db)
    ensure_financial_account(db, order, organization_id=order.organization_id)
    _, plan = _approved_plan(db, manager, order, "30000")
    installment = plan.installments[0]
    payment = Payment(
        organization_id=order.organization_id, service_order_id=order.id, technician_id=tech.id,
        installment_id=installment.id, payment_type=installment.installment_type,
        payment_method="STRIPE_CARD", currency="MXN", gross_amount=Decimal("9000"),
        provider="STRIPE", idempotency_key="reconcile-service-1", status="CHECKOUT_CREATED",
        stripe_checkout_session_id="cs_reconcile_1",
    )
    db.add(payment)
    db.flush()
    session = {
        "id": "cs_reconcile_1", "livemode": False, "status": "complete", "payment_status": "paid",
        "amount_total": 900000, "currency": "mxn", "client_reference_id": str(payment.id),
        "payment_intent": "pi_reconcile_1", "metadata": {
            "payment_id": str(payment.id), "organization_id": str(order.organization_id),
            "payment_type": installment.installment_type, "service_order_id": str(order.id),
            "installment_id": str(installment.id),
        },
    }
    monkeypatch.setattr("app.services.payment_service._stripe_retrieve_checkout_session", lambda _: session)
    assert reconcile_stripe_checkout_payment(db, payment).status == "PAID"
    assert reconcile_stripe_checkout_payment(db, payment).status == "PAID"
    db.commit()
    assert payment.status == "PAID"
    assert installment.status == "PAID"
    assert db.query(ServiceOrderLedgerEntry).filter_by(service_order_id=order.id, entry_type="SERVICE_PAYMENT").count() == 1
    financial = db.query(ServiceOrderFinancial).filter_by(service_order_id=order.id).one()
    assert financial.service_paid_amount == Decimal("9000.00")
    assert financial.service_outstanding_balance == Decimal("21000.00")


def test_reconcile_rejects_unpaid_or_mismatched_checkout_without_financial_effect(db, monkeypatch):
    _, _, manager, _, tech, _, order = scenario(db)
    ensure_financial_account(db, order, organization_id=order.organization_id)
    _, plan = _approved_plan(db, manager, order, "30000")
    installment = plan.installments[0]
    payment = Payment(
        organization_id=order.organization_id, service_order_id=order.id, technician_id=tech.id,
        installment_id=installment.id, payment_type=installment.installment_type,
        payment_method="STRIPE_CARD", currency="MXN", gross_amount=Decimal("9000"),
        provider="STRIPE", idempotency_key="reconcile-service-2", status="CHECKOUT_CREATED",
        stripe_checkout_session_id="cs_reconcile_2",
    )
    db.add(payment)
    db.flush()
    monkeypatch.setattr("app.services.payment_service._stripe_retrieve_checkout_session", lambda _: {
        "id": "cs_reconcile_2", "livemode": False, "status": "complete", "payment_status": "unpaid",
        "amount_total": 900000, "currency": "mxn", "client_reference_id": str(payment.id),
        "metadata": {"payment_id": str(payment.id), "organization_id": str(order.organization_id), "payment_type": installment.installment_type},
    })
    with pytest.raises(ValueError, match="not paid"):
        reconcile_stripe_checkout_payment(db, payment)
    assert payment.status == "CHECKOUT_CREATED"
    assert installment.status == "AVAILABLE"
    assert db.query(ServiceOrderLedgerEntry).filter_by(entry_type="SERVICE_PAYMENT").count() == 0
