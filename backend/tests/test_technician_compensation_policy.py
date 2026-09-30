from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main  # noqa: F401
from app.database.connection import Base
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.service_order import ServiceOrder
from app.models.service_order_quote import ServiceOrderQuote, ServiceOrderQuoteItem
from app.models.technician_compensation import (
    ServiceOrderCompensationSnapshot,
    TechnicianCompensationPolicy,
)
from app.models.technician_earning import TechnicianEarning
from app.models.user import User
from app.services.technician_compensation_service import (
    CompensationError,
    activate_policy,
    create_policy,
    freeze_snapshot,
    preview_order,
    propose_snapshot,
    ensure_technician_acceptance_allowed,
    list_technician_snapshots,
    technician_snapshot_payload,
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
        engine.dispose()


def make_user(db, org, username, *, role="TECNICO"):
    row = User(
        organization_id=org.id, username=username, email=f"{username}@example.test",
        password_hash="synthetic", role=role, status="ACTIVE", is_active=True,
        email_verified=True,
    )
    db.add(row)
    db.flush()
    return row


def make_fixture(db):
    org = Organization(name="compensation-test", slug="compensation-test", status="ACTIVE")
    db.add(org)
    db.flush()
    manager = make_user(db, org, "manager", role="GERENTE")
    technician = make_user(db, org, "technician")
    db.add(OrganizationMembership(
        user_id=technician.id, organization_id=org.id, membership_type="TECHNICIAN",
        role="TECHNICIAN", status="ACTIVE", is_primary=True, is_operational=True,
    ))
    order = ServiceOrder(
        organization_id=org.id, lead_id=1, order_number="COMP-1",
        responsible_user_id=technician.id, status="ASSIGNED", warranty_days=0,
        created_at=datetime.utcnow(),
    )
    db.add(order)
    db.flush()
    quote = ServiceOrderQuote(
        service_order_id=order.id, organization_id=org.id, version=1, status="APPROVED",
        subtotal=Decimal("1400.00"), discount_amount=Decimal("100.00"), tax_amount=Decimal("50.00"),
        total=Decimal("1350.00"), currency="MXN", created_by_user_id=manager.id,
        approved_at=datetime.utcnow(), approved_total=Decimal("1350.00"),
    )
    quote.items = [
        ServiceOrderQuoteItem(organization_id=org.id, description="labor", quantity=1, unit="service", unit_price=Decimal("1000.00"), subtotal=Decimal("1000.00"), compensation_category="LABOR"),
        ServiceOrderQuoteItem(organization_id=org.id, description="material", quantity=1, unit="piece", unit_price=Decimal("400.00"), subtotal=Decimal("400.00"), compensation_category="MATERIAL"),
    ]
    db.add(quote)
    db.commit()
    return org, manager, technician, order


def make_active_policy(db, manager, org):
    row = create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from=datetime.utcnow() - timedelta(minutes=1), idempotency_key="policy-v1",
    )
    activate_policy(db, actor=manager, policy_id=row.id)
    db.commit()
    return row


def test_feature_is_off_by_default(monkeypatch, db):
    monkeypatch.delenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", raising=False)
    org, manager, _, _ = make_fixture(db)
    with pytest.raises(CompensationError, match="COMPENSATION_POLICY_UNAVAILABLE"):
        create_policy(db, actor=manager, organization_id=org.id, currency="MXN", effective_from=datetime.utcnow(), idempotency_key="off")


def test_preview_uses_labor_only_and_keeps_decimal_values(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, technician, order = make_fixture(db)
    make_active_policy(db, manager, org)
    result = preview_order(db, actor=manager, order_id=order.id)
    assert result["labor_base_amount"] == "1000.00"
    assert result["material_amount"] == "400.00"
    assert result["tax_amount"] == "50.00"
    assert result["discount_amount"] == "100.00"
    assert result["technician_amount"] == "750.00"
    assert result["organization_amount"] == "250.00"
    assert result["guarantee_days"] == 7
    assert technician.id == result["technician_user_id"]


def test_other_or_excess_discount_fails_closed(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, order = make_fixture(db)
    make_active_policy(db, manager, org)
    quote = db.query(ServiceOrderQuote).first()
    quote.items[0].compensation_category = "OTHER"
    db.commit()
    with pytest.raises(CompensationError, match="ITEM_CLASSIFICATION_REQUIRED"):
        propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="other")
    quote.items[0].compensation_category = "LABOR"
    quote.discount_amount = Decimal("251.00")
    db.commit()
    with pytest.raises(CompensationError, match="DISCOUNT_EXCEEDS_ORGANIZATION_SHARE"):
        propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="discount")


def test_snapshot_freeze_is_immutable_and_idempotent(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, technician, order = make_fixture(db)
    make_active_policy(db, manager, org)
    proposed = propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="snapshot-1")
    assert proposed.status == "PROPOSED"
    frozen = freeze_snapshot(db, actor=manager, snapshot_id=proposed.id)
    db.commit()
    assert frozen.status == "FROZEN"
    assert frozen.approved_by == manager.id
    assert propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="snapshot-1").id == proposed.id
    assert db.query(TechnicianEarning).count() == 0
    technician.role = "GERENTE"
    db.flush()
    with pytest.raises(CompensationError, match="APPROVER_CANNOT_BE_TECHNICIAN"):
        freeze_snapshot(db, actor=technician, snapshot_id=proposed.id)


def test_new_snapshot_voids_previous_version(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, order = make_fixture(db)
    make_active_policy(db, manager, org)
    first = propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="snapshot-a")
    second = propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="snapshot-b")
    db.commit()
    assert db.get(ServiceOrderCompensationSnapshot, first.id).status == "VOID"
    assert second.status == "PROPOSED"


def test_policy_requires_exact_share_total(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    with pytest.raises(CompensationError, match="SHARE_TOTAL_INVALID"):
        create_policy(db, actor=manager, organization_id=org.id, currency="MXN", effective_from=datetime.utcnow(), idempotency_key="bad", technician_share_bps=7000, organization_share_bps=2000)


def test_startup_excludes_compensation_tables():
    from app.main import STARTUP_MANUAL_MIGRATION_TABLES
    assert {
        "technician_compensation_policies",
        "service_order_compensation_snapshots",
        "technician_compensation_events",
    }.issubset(STARTUP_MANUAL_MIGRATION_TABLES)


def test_technician_view_is_frozen_only_and_omits_internal_margin(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, technician, order = make_fixture(db)
    make_active_policy(db, manager, org)
    proposed = propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="technician-view")
    db.commit()
    assert list_technician_snapshots(db, actor=technician) == []
    freeze_snapshot(db, actor=manager, snapshot_id=proposed.id)
    db.commit()
    rows = list_technician_snapshots(db, actor=technician)
    payload = technician_snapshot_payload(rows[0])
    assert payload["status"] == "FROZEN"
    assert payload["technician_amount"] == "750.00"
    assert "organization_amount" not in payload
    assert "organization_share_bps" not in payload
    assert "idempotency_key_hash" not in payload


def test_acceptance_requires_frozen_snapshot_when_policy_enabled(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, technician, order = make_fixture(db)
    make_active_policy(db, manager, org)
    with pytest.raises(CompensationError, match="COMPENSATION_SNAPSHOT_REQUIRED"):
        ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)
    proposed = propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="acceptance-gate")
    freeze_snapshot(db, actor=manager, snapshot_id=proposed.id)
    db.commit()
    ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)


def test_unquoted_assignment_is_not_blocked_by_compensation_gate(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, _, technician, order = make_fixture(db)
    db.query(ServiceOrderQuote).delete()
    db.commit()
    ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)


def test_public_config_and_frontend_gate_keep_compensation_off(monkeypatch):
    from app.auth.routes import public_config

    monkeypatch.delenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", raising=False)
    assert public_config().technician_compensation_enabled is False
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    assert public_config().technician_compensation_enabled is True

    frontend = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    guard = "if (authPublicConfig.technician_compensation_enabled !== true) return;"
    assert guard in frontend
    assert f"{guard}\n      await Promise.all([loadTechnicianCompensationAdmin(), loadTechnicianCompensationTechnician()]);" in frontend
