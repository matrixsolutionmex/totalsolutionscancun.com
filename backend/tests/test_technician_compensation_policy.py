from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
    TechnicianCompensationEvent,
    TechnicianCompensationPolicy,
)
from app.models.technician_earning import TechnicianEarning
from app.models.user import User
from app.services.technician_compensation_service import (
    CompensationError,
    activate_policy,
    compensation_policy_enabled_for_organization,
    create_policy,
    normalize_effective_from,
    freeze_snapshot,
    preview_order,
    propose_snapshot,
    resolve_policy_for_order,
    ensure_technician_acceptance_allowed,
    list_technician_snapshots,
    technician_snapshot_payload,
    void_policy,
)


def utcnow():
    return datetime.now(timezone.utc)


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


@pytest.fixture(autouse=True)
def compensation_rollout_defaults(monkeypatch):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", "all")
    monkeypatch.delenv("TECHNICIAN_COMPENSATION_POLICY_CANARY_ORGANIZATION_IDS", raising=False)


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
        created_at=utcnow(),
    )
    db.add(order)
    db.flush()
    quote = ServiceOrderQuote(
        service_order_id=order.id, organization_id=org.id, version=1, status="APPROVED",
        subtotal=Decimal("1400.00"), discount_amount=Decimal("100.00"), tax_amount=Decimal("50.00"),
        total=Decimal("1350.00"), currency="MXN", created_by_user_id=manager.id,
        approved_at=utcnow(), approved_total=Decimal("1350.00"),
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
        effective_from=utcnow() + timedelta(minutes=1), idempotency_key="policy-v1",
    )
    activate_policy(db, actor=manager, policy_id=row.id)
    order = db.query(ServiceOrder).filter_by(organization_id=org.id).first()
    quote = db.query(ServiceOrderQuote).filter_by(service_order_id=order.id).first()
    order.created_at = row.effective_from + timedelta(seconds=1)
    quote.approved_at = order.created_at
    db.commit()
    return row


def test_feature_is_off_by_default(monkeypatch, db):
    monkeypatch.delenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", raising=False)
    org, manager, _, _ = make_fixture(db)
    with pytest.raises(CompensationError, match="COMPENSATION_POLICY_UNAVAILABLE"):
        create_policy(db, actor=manager, organization_id=org.id, currency="MXN", effective_from=utcnow(), idempotency_key="off")


def test_rollout_is_organization_scoped_and_fail_closed(monkeypatch, db):
    org, manager, _, order = make_fixture(db)
    other_org = Organization(name="other", slug="other", status="ACTIVE")
    db.add(other_org)
    db.flush()

    monkeypatch.delenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", raising=False)
    assert compensation_policy_enabled_for_organization(db, org.id) is False

    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    monkeypatch.delenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", raising=False)
    assert compensation_policy_enabled_for_organization(db, org.id) is False

    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_CANARY_ORGANIZATION_IDS", f"  {org.id}, {org.id} ")
    assert compensation_policy_enabled_for_organization(db, org.id) is True
    assert compensation_policy_enabled_for_organization(db, other_org.id) is False
    with pytest.raises(CompensationError, match="COMPENSATION_POLICY_UNAVAILABLE"):
        create_policy(db, actor=manager, organization_id=other_org.id, currency="MXN", effective_from=utcnow(), idempotency_key="outside")

    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", "invalid")
    assert compensation_policy_enabled_for_organization(db, org.id) is False
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_CANARY_ORGANIZATION_IDS", "0,abc")
    assert compensation_policy_enabled_for_organization(db, org.id) is False
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", "all")
    assert compensation_policy_enabled_for_organization(db, other_org.id) is True
    assert order.organization_id == org.id


def test_marketplace_gate_only_applies_inside_canary(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, _, technician, order = make_fixture(db)
    manager = db.query(User).filter_by(organization_id=org.id, role="GERENTE").first()
    make_active_policy(db, manager, org)
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_CANARY_ORGANIZATION_IDS", str(org.id + 1))
    ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)

    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_CANARY_ORGANIZATION_IDS", str(org.id))
    with pytest.raises(CompensationError, match="COMPENSATION_SNAPSHOT_REQUIRED"):
        ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)


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
        create_policy(db, actor=manager, organization_id=org.id, currency="MXN", effective_from=utcnow() + timedelta(minutes=1), idempotency_key="bad", technician_share_bps=7000, organization_share_bps=2000)


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


def test_effective_from_requires_timezone_aware_utc(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    with pytest.raises(CompensationError, match="EFFECTIVE_FROM_TIMEZONE_REQUIRED"):
        create_policy(
            db, actor=manager, organization_id=org.id, currency="MXN",
            effective_from=datetime.utcnow(), idempotency_key="naive-cutoff",
        )


def test_local_cutoff_converts_cancun_to_canonical_utc(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    expected_utc = (utcnow() + timedelta(days=1)).replace(second=0, microsecond=0)
    local_cutoff = expected_utc.astimezone(ZoneInfo("America/Cancun")).replace(tzinfo=None)
    policy = create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from_local=local_cutoff.isoformat(timespec="minutes"), effective_timezone="America/Cancun",
        idempotency_key="cancun-cutoff",
    )
    assert policy.effective_from == expected_utc.replace(tzinfo=None)


def test_cutoff_requires_explicit_input(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    with pytest.raises(CompensationError, match="EFFECTIVE_FROM_REQUIRED"):
        create_policy(db, actor=manager, organization_id=org.id, currency="MXN", idempotency_key="missing-cutoff")


@pytest.mark.parametrize("timezone_name, local, expected", [
    ("Not/AZone", "2026-10-05T09:00", "EFFECTIVE_FROM_TIMEZONE_INVALID"),
    ("America/New_York", "2026-03-08T02:30", "EFFECTIVE_FROM_LOCAL_NONEXISTENT"),
])
def test_invalid_local_cutoff_fails_closed(monkeypatch, db, timezone_name, local, expected):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    with pytest.raises(CompensationError, match=expected):
        create_policy(
            db, actor=manager, organization_id=org.id, currency="MXN",
            effective_from_local=local, effective_timezone=timezone_name,
            idempotency_key=f"invalid-{timezone_name}",
        )
    assert db.query(TechnicianCompensationPolicy).count() == 0


def test_ambiguous_local_cutoff_fails_closed():
    with pytest.raises(CompensationError, match="EFFECTIVE_FROM_LOCAL_AMBIGUOUS"):
        normalize_effective_from(effective_from_local="2026-11-01T01:30", effective_timezone="America/New_York")


def test_local_cutoff_rejects_conflicting_utc_input():
    with pytest.raises(CompensationError, match="EFFECTIVE_FROM_INPUT_CONFLICT"):
        normalize_effective_from(
            effective_from=datetime(2026, 10, 5, 14, 0, tzinfo=ZoneInfo("UTC")),
            effective_from_local="2026-10-05T09:00", effective_timezone="America/Cancun",
        )


def test_cutoff_boundaries_keep_old_orders_legacy(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, technician, order = make_fixture(db)
    cutoff = utcnow() + timedelta(minutes=5)
    policy = create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from=cutoff, idempotency_key="cutoff-v1",
    )
    activate_policy(db, actor=manager, policy_id=policy.id)
    quote = db.query(ServiceOrderQuote).filter_by(service_order_id=order.id).first()

    order.created_at = policy.effective_from - timedelta(seconds=1)
    quote.approved_at = order.created_at
    db.commit()
    assert resolve_policy_for_order(db, order) is None
    ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)

    order.created_at = policy.effective_from
    quote.approved_at = order.created_at
    db.commit()
    assert resolve_policy_for_order(db, order).id == policy.id
    with pytest.raises(CompensationError, match="COMPENSATION_SNAPSHOT_REQUIRED"):
        ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)

    order.created_at = policy.effective_from + timedelta(seconds=1)
    quote.approved_at = order.created_at
    db.commit()
    assert resolve_policy_for_order(db, order).id == policy.id


def test_first_policy_cannot_be_backdated(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    with pytest.raises(CompensationError, match="POLICY_EFFECTIVE_FROM_PAST"):
        create_policy(
            db, actor=manager, organization_id=org.id, currency="MXN",
            effective_from=utcnow() - timedelta(seconds=1), idempotency_key="backdated",
        )
    assert db.query(TechnicianCompensationPolicy).count() == 0


def test_retired_versions_resolve_by_order_cutoff(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, order = make_fixture(db)
    first_cutoff = utcnow() + timedelta(minutes=5)
    first = create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from=first_cutoff, idempotency_key="version-one",
    )
    activate_policy(db, actor=manager, policy_id=first.id)
    quote = db.query(ServiceOrderQuote).filter_by(service_order_id=order.id).first()
    order.created_at = first.effective_from + timedelta(seconds=1)
    quote.approved_at = order.created_at
    db.commit()

    second_cutoff = first_cutoff + timedelta(days=1)
    second = create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from=second_cutoff, idempotency_key="version-two",
    )
    activate_policy(db, actor=manager, policy_id=second.id)
    db.commit()
    assert db.get(TechnicianCompensationPolicy, first.id).status == "RETIRED"

    assert resolve_policy_for_order(db, order).id == first.id
    order.created_at = second.effective_from
    quote.approved_at = order.created_at
    db.commit()
    assert resolve_policy_for_order(db, order).id == second.id


def make_draft_policy(db, manager, org, key="voidable"):
    return create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from=utcnow() + timedelta(minutes=10), idempotency_key=key,
    )


def test_draft_policy_can_be_voided_with_a_controlled_reason(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    policy = make_draft_policy(db, manager, org)
    voided = void_policy(db, actor=manager, policy_id=policy.id, reason_code="incorrect_cutoff", idempotency_key="void-1")
    db.commit()
    assert voided.status == "VOID"
    event = db.query(TechnicianCompensationEvent).filter_by(policy_id=policy.id, event_type="POLICY_VOIDED").one()
    assert event.reason_code == "INCORRECT_CUTOFF"
    assert db.query(TechnicianCompensationPolicy).count() == 1


def test_policy_void_is_idempotent_and_conflicting_replay_is_rejected(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    policy = make_draft_policy(db, manager, org, "void-idempotent")
    first = void_policy(db, actor=manager, policy_id=policy.id, reason_code="CREATED_IN_ERROR", idempotency_key="void-key")
    db.commit()
    second = void_policy(db, actor=manager, policy_id=policy.id, reason_code="CREATED_IN_ERROR", idempotency_key="void-key")
    assert second.id == first.id
    with pytest.raises(CompensationError, match="IDEMPOTENCY_CONFLICT"):
        void_policy(db, actor=manager, policy_id=policy.id, reason_code="DUPLICATE_DRAFT", idempotency_key="void-key")
    assert db.query(TechnicianCompensationEvent).filter_by(event_type="POLICY_VOIDED").count() == 1


@pytest.mark.parametrize("status", ["ACTIVE", "RETIRED", "VOID"])
def test_only_drafts_can_be_voided(monkeypatch, db, status):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, _ = make_fixture(db)
    policy = make_draft_policy(db, manager, org, f"void-status-{status}")
    policy.status = status
    db.commit()
    with pytest.raises(CompensationError, match="POLICY_NOT_DRAFT"):
        void_policy(db, actor=manager, policy_id=policy.id, reason_code="CONFIGURATION_ERROR", idempotency_key=f"void-{status}")


def test_invalid_void_reason_and_non_admin_are_rejected(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, technician, _ = make_fixture(db)
    policy = make_draft_policy(db, manager, org, "void-security")
    with pytest.raises(CompensationError, match="VOID_REASON_INVALID"):
        void_policy(db, actor=manager, policy_id=policy.id, reason_code="FREE_TEXT", idempotency_key="void-invalid")
    with pytest.raises(CompensationError, match="ADMIN_REQUIRED"):
        void_policy(db, actor=technician, policy_id=policy.id, reason_code="CONFIGURATION_ERROR", idempotency_key="void-technician")
    assert policy.status == "DRAFT"


def test_void_policy_is_ignored_and_next_draft_gets_next_version(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, order = make_fixture(db)
    first = make_draft_policy(db, manager, org, "void-v1")
    void_policy(db, actor=manager, policy_id=first.id, reason_code="DUPLICATE_DRAFT", idempotency_key="void-v1-key")
    second = make_draft_policy(db, manager, org, "void-v2")
    assert second.version == 2
    assert resolve_policy_for_order(db, order) is None


def test_pre_policy_snapshot_is_not_created(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, _, order = make_fixture(db)
    policy = create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from=utcnow() + timedelta(minutes=5), idempotency_key="future-only",
    )
    activate_policy(db, actor=manager, policy_id=policy.id)
    order.created_at = policy.effective_from - timedelta(seconds=1)
    quote = db.query(ServiceOrderQuote).filter_by(service_order_id=order.id).first()
    quote.approved_at = order.created_at
    db.commit()
    with pytest.raises(CompensationError, match="PRE_POLICY_LEGACY"):
        propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="retroactive")
    assert db.query(ServiceOrderCompensationSnapshot).count() == 0


def test_snapshot_from_previous_cutoff_is_rejected_for_new_order_period(monkeypatch, db):
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    org, manager, technician, order = make_fixture(db)
    first = make_active_policy(db, manager, org)
    proposed = propose_snapshot(db, actor=manager, order_id=order.id, idempotency_key="old-period")
    freeze_snapshot(db, actor=manager, snapshot_id=proposed.id)
    db.commit()

    second = create_policy(
        db, actor=manager, organization_id=org.id, currency="MXN",
        effective_from=first.effective_from.replace(tzinfo=timezone.utc) + timedelta(days=1),
        idempotency_key="new-period",
    )
    activate_policy(db, actor=manager, policy_id=second.id)
    quote = db.query(ServiceOrderQuote).filter_by(service_order_id=order.id).first()
    order.created_at = second.effective_from + timedelta(seconds=1)
    quote.approved_at = order.created_at
    db.commit()

    with pytest.raises(CompensationError, match="COMPENSATION_SNAPSHOT_REQUIRED"):
        ensure_technician_acceptance_allowed(db, order=order, technician_user_id=technician.id)


def test_public_config_and_frontend_gate_keep_compensation_off(monkeypatch):
    from app.auth.routes import public_config

    monkeypatch.delenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", raising=False)
    assert public_config().technician_compensation_enabled is False
    monkeypatch.setenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "true")
    assert public_config().technician_compensation_enabled is True

    frontend = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    assert "/technician-compensation/availability" in frontend
    assert "payload?.enabled !== true" in frontend
