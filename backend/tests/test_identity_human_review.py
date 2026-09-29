import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main  # noqa: F401 - register the complete model graph
from app.database.connection import Base
from app.models.identity_human_review import IdentityHumanReviewDecision
from app.models.identity_provider import IdentityVerificationAttempt, IdentityVerificationEvent
from app.models.identity_verification import IdentityVerification
from app.models.organization import Organization
from app.models.user import User
from app.services.identity_human_review_service import (
    decide_review,
    get_review_item,
    list_review_queue,
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


def make_org(db, slug):
    row = Organization(name=slug, slug=slug, status="ACTIVE")
    db.add(row)
    db.flush()
    return row


def make_user(db, org, *, role="BROKER", username=None):
    row = User(
        organization_id=org.id,
        username=username or f"review-{role.lower()}-{org.id}-{db.query(User).count()}",
        email=f"{username or 'review'}@example.test",
        password_hash="test-hash",
        role=role,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(row)
    db.flush()
    return row


def make_attempt(db, candidate, org, *, mode="sandbox", status="PENDING_REVIEW"):
    identity = IdentityVerification(user_id=candidate.id, organization_id=org.id, status=status)
    db.add(identity)
    db.flush()
    attempt = IdentityVerificationAttempt(
        user_id=candidate.id,
        organization_id=org.id,
        identity_verification_id=identity.id,
        attempt_key_hash=(f"{candidate.id:064d}")[-64:],
        provider="metamap",
        provider_mode=mode,
        policy="MEXICAN",
        status=status,
        consented_at=datetime.utcnow(),
        consent_version="identity-verification-v1",
        expires_at=datetime.utcnow() + timedelta(minutes=20),
    )
    db.add(attempt)
    db.commit()
    return attempt, identity


def enable_review(monkeypatch):
    monkeypatch.setenv("IDENTITY_HUMAN_REVIEW_ENABLED", "true")


def test_review_is_fail_closed_when_disabled(monkeypatch, db):
    monkeypatch.delenv("IDENTITY_HUMAN_REVIEW_ENABLED", raising=False)
    org = make_org(db, "review-off")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    attempt, _ = make_attempt(db, candidate, org)

    with pytest.raises(RuntimeError):
        list_review_queue(db, reviewer=reviewer)
    assert db.query(IdentityHumanReviewDecision).count() == 0
    assert attempt.status == "PENDING_REVIEW"


def test_queue_is_tenant_and_role_scoped_and_denies_self_review(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "review-org")
    other_org = make_org(db, "other-org")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    other_reviewer = make_user(db, other_org, role="GERENTE")
    technician = make_user(db, org, role="TECNICO")
    attempt, _ = make_attempt(db, candidate, org)

    assert [item["id"] for item in list_review_queue(db, reviewer=reviewer)] == [attempt.id]
    with pytest.raises(PermissionError):
        list_review_queue(db, reviewer=technician)
    with pytest.raises(LookupError):
        get_review_item(db, attempt_id=attempt.id, reviewer=other_reviewer)

    candidate_attempt, _ = make_attempt(db, reviewer, org)
    with pytest.raises(PermissionError):
        get_review_item(db, attempt_id=candidate_attempt.id, reviewer=reviewer)


def test_sandbox_cannot_approve_and_does_not_generate_verified(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "sandbox-review")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    attempt, identity = make_attempt(db, candidate, org, mode="sandbox")

    with pytest.raises(ValueError, match="provider approval"):
        decide_review(
            db,
            attempt_id=attempt.id,
            reviewer=reviewer,
            decision="APPROVE",
            reason_code="PROVIDER_RESULT_REVIEW",
            idempotency_key="sandbox-approve-1",
        )
    db.rollback()
    assert identity.status == "PENDING_REVIEW"
    assert db.query(IdentityHumanReviewDecision).count() == 0


def test_live_approval_requires_persisted_provider_approval(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "live-approval")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    attempt, identity = make_attempt(db, candidate, org, mode="live")

    with pytest.raises(ValueError, match="provider approval"):
        decide_review(
            db,
            attempt_id=attempt.id,
            reviewer=reviewer,
            decision="APPROVE",
            reason_code="PROVIDER_RESULT_REVIEW",
            idempotency_key="live-approve-without-provider-result",
        )
    db.rollback()
    db.add(IdentityVerificationEvent(
        provider="metamap",
        provider_event_id="provider-approved-event",
        identity_verification_attempt_id=attempt.id,
        event_name="verification_completed",
        payload_hash="a" * 64,
        status="PROCESSED",
        reason_code="PROVIDER_APPROVED",
    ))
    db.commit()
    result = decide_review(
        db,
        attempt_id=attempt.id,
        reviewer=reviewer,
        decision="APPROVE",
        reason_code="PROVIDER_RESULT_REVIEW",
        idempotency_key="live-approve-with-provider-result",
    )
    db.commit()
    assert result["status"] == "APPLIED"
    assert identity.status == "VERIFIED"


def test_request_changes_and_reject_are_atomic_state_transitions(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "transition-review")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="ROOT")
    attempt, identity = make_attempt(db, candidate, org)

    result = decide_review(
        db,
        attempt_id=attempt.id,
        reviewer=reviewer,
        decision="REQUEST_CHANGES",
        reason_code="DOCUMENT_CLARIFICATION",
        idempotency_key="request-changes-1",
    )
    db.commit()
    assert result["status"] == "APPLIED"
    assert identity.status == "NEEDS_ACTION"
    assert attempt.status == "IN_PROGRESS"
    with pytest.raises(ValueError, match="not pending"):
        decide_review(
            db,
            attempt_id=attempt.id,
            reviewer=reviewer,
            decision="REJECT",
            reason_code="POLICY_CHECK",
            idempotency_key="reject-after-changes",
        )


def test_live_reject_is_idempotent_and_terminal(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "live-review")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    attempt, identity = make_attempt(db, candidate, org, mode="live")

    first = decide_review(
        db,
        attempt_id=attempt.id,
        reviewer=reviewer,
        decision="REJECT",
        reason_code="INSUFFICIENT_EVIDENCE",
        idempotency_key="reject-1",
    )
    db.commit()
    replay = decide_review(
        db,
        attempt_id=attempt.id,
        reviewer=reviewer,
        decision="REJECT",
        reason_code="INSUFFICIENT_EVIDENCE",
        idempotency_key="reject-1",
    )
    assert replay["status"] == "IDEMPOTENT_REPLAY"
    assert replay["decision_id"] == first["decision_id"]
    assert identity.status == "REJECTED"
    assert attempt.status == "REJECTED"
    with pytest.raises(ValueError, match="already decided"):
        decide_review(
            db,
            attempt_id=attempt.id,
            reviewer=reviewer,
            decision="REJECT",
            reason_code="POLICY_CHECK",
            idempotency_key="reject-2",
        )
    assert db.query(IdentityHumanReviewDecision).count() == 1


def test_terminal_live_approval_leaves_queue(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "approved-queue")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    attempt, identity = make_attempt(db, candidate, org, mode="live")
    db.add(IdentityVerificationEvent(
        provider="metamap",
        provider_event_id="approved-queue-event",
        identity_verification_attempt_id=attempt.id,
        event_name="verification_completed",
        payload_hash="c" * 64,
        status="PROCESSED",
        reason_code="PROVIDER_APPROVED",
    ))
    db.commit()

    decide_review(
        db,
        attempt_id=attempt.id,
        reviewer=reviewer,
        decision="APPROVE",
        reason_code="PROVIDER_RESULT_REVIEW",
        idempotency_key="approved-queue-1",
    )
    db.commit()

    assert identity.status == "VERIFIED"
    assert list_review_queue(db, reviewer=reviewer) == []


def test_invalid_decision_reason_and_non_pending_are_rejected(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "invalid-review")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    attempt, _ = make_attempt(db, candidate, org, status="IN_PROGRESS")

    with pytest.raises(ValueError, match="invalid review"):
        decide_review(db, attempt_id=attempt.id, reviewer=reviewer, decision="APPROVE", reason_code="FREE_TEXT", idempotency_key="bad-1")
    with pytest.raises(ValueError, match="not pending"):
        get_review_item(db, attempt_id=attempt.id, reviewer=reviewer)


def test_safe_payload_has_no_provider_or_personal_data(monkeypatch, db):
    enable_review(monkeypatch)
    org = make_org(db, "safe-review")
    candidate = make_user(db, org)
    reviewer = make_user(db, org, role="GERENTE")
    attempt, _ = make_attempt(db, candidate, org)
    payload = get_review_item(db, attempt_id=attempt.id, reviewer=reviewer)
    serialized = repr(payload)
    for forbidden in (candidate.email, "document", "curp", "selfie", "biometric", "secret", "payload", "hash"):
        assert forbidden not in serialized.lower()


def test_migration_087_is_manual_and_has_terminal_guard():
    migration = (os.path.dirname(__file__) + "/../migrations/087_identity_human_review.sql")
    source = open(migration, encoding="utf-8").read()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_review_terminal_attempt" in source
    assert "decision IN ('APPROVE', 'REJECT')" in source
    assert "IDENTITY_HUMAN_REVIEW_ENABLED" not in source


def test_startup_excludes_human_review_table_from_create_all():
    source = open(os.path.dirname(__file__) + "/../app/main.py", encoding="utf-8").read()
    assert '"identity_human_review_decisions"' in source


def test_human_review_frontend_is_fail_closed_and_local_only():
    source = Path(__file__).parents[2].joinpath("frontend/index.html").read_text(encoding="utf-8")
    assert source.count("identityReviewTitle") >= 3
    assert '<section class="panel-box identity-human-review-panel" id="identityHumanReviewPanel" hidden' in source
    assert 'id="identityHumanReviewModal" hidden' in source
    assert "resetHumanReviewUi();" in source
    assert "response.status === 409" in source
    assert "data-human-review-decision" in source
    review_logic = source.split("const HUMAN_REVIEW_DECISIONS", 1)[1].split("function loadMetaMapSdk", 1)[0]
    for forbidden in ("web-button.metamap.com", "user_id", "organization_id", "curp", "selfie", "secret"):
        assert forbidden not in review_logic.lower()


def test_human_review_actions_never_optimistically_mark_verified():
    source = Path(__file__).parents[2].joinpath("frontend/index.html").read_text(encoding="utf-8")
    review_logic = source.split("const HUMAN_REVIEW_DECISIONS", 1)[1].split("function loadMetaMapSdk", 1)[0]
    assert "button.disabled = !item.reviewable || sandboxApproval" in review_logic
    assert "await loadHumanReviewQueue();" in review_logic
    assert "identityVerificationPayload" not in review_logic
