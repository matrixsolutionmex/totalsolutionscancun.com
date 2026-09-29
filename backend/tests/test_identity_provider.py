import hashlib
import hmac
import json
import os
from uuid import uuid4
from datetime import datetime, timedelta
from pathlib import Path

import app.main  # noqa: F401 - register the complete model graph
import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.identity_provider import IdentityVerificationAttempt, IdentityVerificationEvent
from app.models.identity_verification import IdentityVerification
from app.models.organization import Organization
from app.models.user import User
from app.services.identity_provider_service import (
    CONSENT_VERSION,
    create_attempt,
    process_metamap_event,
    provider_config,
    rollout_config,
    verify_metamap_signature,
)


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_085 = (MIGRATIONS_DIR / "085_identity_verification_foundation.sql").read_text()
MIGRATION_086 = (MIGRATIONS_DIR / "086_identity_provider_sandbox.sql").read_text()


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


@pytest.fixture()
def postgres_identity_migration_db():
    database_url = os.getenv("IDENTITY_MIGRATION_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("IDENTITY_MIGRATION_TEST_DATABASE_URL is required for PostgreSQL migration checks")
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    schema = f"identity_086_{uuid4().hex}"
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            cursor.execute("CREATE TABLE organizations (id INTEGER PRIMARY KEY)")
            cursor.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            cursor.execute(MIGRATION_085)
        yield connection
    finally:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        connection.close()


def seed_postgres_identity_parent_rows(connection):
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO organizations(id) VALUES (1)")
        cursor.execute("INSERT INTO users(id) VALUES (1)")
        cursor.execute(
            "INSERT INTO identity_verifications(id, user_id, organization_id, status) "
            "VALUES (1, 1, 1, 'NOT_STARTED')"
        )


def insert_postgres_attempt(connection, *, attempt_hash: str, provider: str, status: str):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO identity_verification_attempts (
                user_id, organization_id, identity_verification_id, attempt_key_hash,
                provider, provider_mode, policy, status, consented_at, consent_version, expires_at
            ) VALUES (1, 1, 1, %s, %s, 'sandbox', 'MEXICAN', %s, CURRENT_TIMESTAMP, 'v1', CURRENT_TIMESTAMP + interval '20 minutes')
            """,
            (attempt_hash, provider, status),
        )


def test_migration_086_postgresql_rejects_invalid_status_and_provider_and_has_status_index(postgres_identity_migration_db):
    connection = postgres_identity_migration_db
    with connection.cursor() as cursor:
        cursor.execute(MIGRATION_086)
    seed_postgres_identity_parent_rows(connection)
    insert_postgres_attempt(connection, attempt_hash="a" * 64, provider="metamap", status="CREATED")
    with pytest.raises(psycopg2.errors.CheckViolation):
        insert_postgres_attempt(connection, attempt_hash="b" * 64, provider="metamap", status="INVALID")
    for invalid_provider, attempt_hash in (("", "c" * 64), ("other", "d" * 64)):
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert_postgres_attempt(connection, attempt_hash=attempt_hash, provider=invalid_provider, status="CREATED")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = 'identity_verification_attempts' "
            "AND indexname = 'ix_identity_verification_attempts_status'"
        )
        assert cursor.fetchone()[0] == 1


def test_migration_086_postgresql_rolls_back_and_reapplies(postgres_identity_migration_db):
    connection = postgres_identity_migration_db
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(MIGRATION_086)
            cursor.execute(
                "SELECT COUNT(*) FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename IN ('identity_verification_attempts', 'identity_verification_events')"
            )
            assert cursor.fetchone()[0] == 2
        connection.rollback()
    finally:
        connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname = current_schema() "
            "AND tablename IN ('identity_verification_attempts', 'identity_verification_events')"
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(MIGRATION_086)
        cursor.execute(MIGRATION_086)
        cursor.execute(
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname = current_schema() "
            "AND tablename IN ('identity_verification_attempts', 'identity_verification_events')"
        )
        assert cursor.fetchone()[0] == 2


def make_user(db, organization_id):
    user = User(
        organization_id=organization_id,
        username="provider-user",
        email="provider@example.test",
        password_hash="hash",
        role="TECNICO",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.flush()
    return user


def configure_sandbox(monkeypatch):
    monkeypatch.setenv("IDENTITY_VERIFICATION_UI_ENABLED", "true")
    monkeypatch.setenv("IDENTITY_PROVIDER", "metamap")
    monkeypatch.setenv("IDENTITY_PROVIDER_MODE", "sandbox")
    monkeypatch.setenv("METAMAP_CLIENT_ID", "client-public-test")
    monkeypatch.setenv("METAMAP_FLOW_ID", "flow-test")
    monkeypatch.setenv("METAMAP_WEBHOOK_SECRET", "WebhookSecret123")
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "all")


def test_csp_allows_only_required_metamap_frame_origin():
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    frame_src = source.split('"frame-src ', 1)[1].split('; ', 1)[0].split()
    assert "https://signup.metamap.com" in frame_src
    assert "https://challenges.cloudflare.com" in frame_src
    assert "https://accounts.google.com" in frame_src
    assert not any("*" in origin for origin in frame_src)


def test_provider_is_disabled_without_local_flag(monkeypatch):
    monkeypatch.delenv("IDENTITY_VERIFICATION_UI_ENABLED", raising=False)
    monkeypatch.setenv("IDENTITY_PROVIDER", "metamap")
    monkeypatch.setenv("IDENTITY_PROVIDER_MODE", "sandbox")
    assert provider_config(1, "TECNICO")["enabled"] is False


def test_provider_is_disabled_when_sandbox_configuration_is_incomplete(monkeypatch):
    monkeypatch.setenv("IDENTITY_VERIFICATION_UI_ENABLED", "true")
    monkeypatch.setenv("IDENTITY_PROVIDER", "metamap")
    monkeypatch.setenv("IDENTITY_PROVIDER_MODE", "sandbox")
    monkeypatch.setenv("METAMAP_CLIENT_ID", "client-public-test")
    monkeypatch.delenv("METAMAP_FLOW_ID", raising=False)
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "all")
    assert provider_config(1, "TECNICO")["enabled"] is False


@pytest.mark.parametrize("raw_ids", ["0", "-1", "12x", "1, ,2", "1,2x"])
def test_invalid_canary_ids_fail_closed(monkeypatch, raw_ids):
    configure_sandbox(monkeypatch)
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("IDENTITY_VERIFICATION_CANARY_USER_IDS", raw_ids)
    assert rollout_config()[2] is False
    assert provider_config(1, "TECNICO")["enabled"] is False


def test_canary_ids_normalize_spaces_and_duplicates(monkeypatch):
    configure_sandbox(monkeypatch)
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("IDENTITY_VERIFICATION_CANARY_USER_IDS", " 7, 7,8 ")
    assert rollout_config() == ("canary", frozenset({7, 8}), True)
    assert provider_config(7, "TECNICO")["enabled"] is True
    assert provider_config(8, "BROKER")["enabled"] is True
    assert provider_config(9, "TECNICO")["enabled"] is False
    assert provider_config(9, "TECNICO")["client_id"] is None
    assert provider_config(9, "TECNICO")["flow_id"] is None


def test_canary_allowlist_never_reaches_frontend_source(monkeypatch):
    source = (Path(__file__).resolve().parents[1] / "../frontend/index.html").resolve().read_text()
    assert "IDENTITY_VERIFICATION_CANARY_USER_IDS" not in source


@pytest.mark.parametrize("role", ["ROOT", "ADMIN"])
def test_canary_does_not_allow_root_or_admin_bypass(monkeypatch, role):
    configure_sandbox(monkeypatch)
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("IDENTITY_VERIFICATION_CANARY_USER_IDS", "44")
    assert provider_config(44, role)["enabled"] is False


def test_rollout_defaults_and_off_mode_fail_closed(monkeypatch):
    for name in (
        "IDENTITY_VERIFICATION_UI_ENABLED",
        "IDENTITY_VERIFICATION_ROLLOUT_MODE",
        "IDENTITY_VERIFICATION_CANARY_USER_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert rollout_config() == ("off", frozenset(), True)
    monkeypatch.setenv("IDENTITY_VERIFICATION_UI_ENABLED", "true")
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "off")
    assert provider_config(1, "TECNICO")["enabled"] is False


def test_invalid_rollout_mode_and_removed_user_fail_closed(monkeypatch):
    configure_sandbox(monkeypatch)
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "unexpected")
    assert provider_config(1, "TECNICO")["enabled"] is False
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("IDENTITY_VERIFICATION_CANARY_USER_IDS", "1")
    assert provider_config(2, "TECNICO")["enabled"] is False


def test_malformed_allowlist_fails_closed_in_every_rollout_mode(monkeypatch):
    configure_sandbox(monkeypatch)
    monkeypatch.setenv("IDENTITY_VERIFICATION_CANARY_USER_IDS", "12x")
    for mode in ("off", "all"):
        monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", mode)
        assert rollout_config()[2] is False
        assert provider_config(1, "TECNICO")["enabled"] is False


def test_all_mode_only_allows_identity_eligible_roles(monkeypatch):
    configure_sandbox(monkeypatch)
    assert provider_config(1, "TECNICO")["enabled"] is True
    assert provider_config(2, "BROKER")["enabled"] is True
    assert provider_config(3, "GERENTE")["enabled"] is False
    assert provider_config(4, "ROOT")["enabled"] is False


def test_unauthorized_user_cannot_create_attempt(db, monkeypatch):
    configure_sandbox(monkeypatch)
    monkeypatch.setenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("IDENTITY_VERIFICATION_CANARY_USER_IDS", "999")
    org = Organization(name="Blocked Org", slug="blocked-org")
    db.add(org)
    db.flush()
    user = make_user(db, org.id)
    with pytest.raises(RuntimeError, match="unavailable"):
        create_attempt(db, user=user, organization_id=org.id, policy="MEXICAN", consent_version=CONSENT_VERSION)


def test_attempt_requires_consent_and_stores_only_hash(db, monkeypatch):
    configure_sandbox(monkeypatch)
    org = Organization(name="Provider Org", slug="provider-org")
    db.add(org)
    db.flush()
    user = make_user(db, org.id)
    attempt, opaque, config = create_attempt(db, user=user, organization_id=org.id, policy="MEXICAN", consent_version=CONSENT_VERSION)
    assert opaque not in attempt.attempt_key_hash
    assert config["metadata"] == {"attempt_key": opaque}
    assert config["provider"] == "metamap"
    assert attempt.consent_version == CONSENT_VERSION
    assert attempt.expires_at > datetime.utcnow()


def test_invalid_signature_is_rejected():
    body = b'{"eventName":"verification_completed"}'
    assert verify_metamap_signature(body, "bad", "WebhookSecret123") is False
    expected = hmac.new(b"WebhookSecret123", body, hashlib.sha256).hexdigest()
    assert verify_metamap_signature(body, expected, "WebhookSecret123") is True
    assert verify_metamap_signature(body, expected[:-1], "WebhookSecret123") is False
    assert verify_metamap_signature(body, "z" * 64, "WebhookSecret123") is False


def seed_attempt(db, monkeypatch, org_name="Provider Org", slug="provider-org"):
    configure_sandbox(monkeypatch)
    org = Organization(name=org_name, slug=slug)
    db.add(org)
    db.flush()
    user = make_user(db, org.id)
    attempt, opaque, _ = create_attempt(db, user=user, organization_id=org.id, policy="MEXICAN", consent_version=CONSENT_VERSION)
    db.commit()
    return attempt, opaque, user


def test_sandbox_success_is_never_verified_and_duplicate_is_idempotent(db, monkeypatch):
    attempt, opaque, _ = seed_attempt(db, monkeypatch)
    payload = {"eventName": "verification_completed", "verificationId": "verification-1", "verificationStatus": "verified", "metadata": {"attempt_key": opaque}}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert process_metamap_event(db, raw_body=raw, payload=payload) == "PROCESSED"
    db.commit()
    identity = db.query(IdentityVerification).one()
    assert identity.status == "PENDING_REVIEW"
    assert db.query(IdentityVerificationEvent).count() == 1
    assert process_metamap_event(db, raw_body=raw, payload=payload) == "DUPLICATE"
    assert db.query(IdentityVerificationEvent).count() == 1


def test_unknown_attempt_and_missing_metadata_fail_closed(db, monkeypatch):
    seed_attempt(db, monkeypatch)
    payload = {"eventName": "verification_completed", "verificationId": "verification-2", "metadata": {}}
    with pytest.raises(ValueError, match="invalid identity provider event"):
        process_metamap_event(db, raw_body=json.dumps(payload).encode(), payload=payload)


def test_tenant_binding_rejects_other_attempt_and_expired_attempt(db, monkeypatch):
    attempt, opaque, user = seed_attempt(db, monkeypatch)
    other_org = Organization(name="Other Org", slug="other-org")
    db.add(other_org)
    db.flush()
    attempt.organization_id = other_org.id
    db.commit()
    payload = {"eventName": "verification_started", "verificationId": "verification-3", "metadata": {"attempt_key": opaque}}
    with pytest.raises(Exception):
        process_metamap_event(db, raw_body=json.dumps(payload).encode(), payload=payload)
    db.rollback()
    attempt.organization_id = user.organization_id
    attempt.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    with pytest.raises(ValueError, match="expired"):
        process_metamap_event(db, raw_body=json.dumps(payload).encode(), payload=payload)


def test_webhook_without_provider_event_id_is_deduplicated(db, monkeypatch):
    _, opaque, _ = seed_attempt(db, monkeypatch, org_name="Derived Org", slug="derived-org")
    payload = {
        "eventName": "verification_started",
        "timestamp": "2026-09-28T12:00:00Z",
        "metadata": {"attempt_key": opaque},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert process_metamap_event(db, raw_body=raw, payload=payload) == "PROCESSED"
    db.commit()
    assert process_metamap_event(db, raw_body=raw, payload=payload) == "DUPLICATE"
    assert db.query(IdentityVerificationEvent).count() == 1


def test_invalid_transition_and_metadata_fail_closed(db, monkeypatch):
    _, opaque, _ = seed_attempt(db, monkeypatch, org_name="Transition Org", slug="transition-org")
    completed = {
        "eventName": "verification_completed",
        "verificationId": "transition-1",
        "verificationStatus": "verified",
        "metadata": {"attempt_key": opaque},
    }
    raw_completed = json.dumps(completed, separators=(",", ":")).encode()
    assert process_metamap_event(db, raw_body=raw_completed, payload=completed) == "PROCESSED"
    db.commit()
    started = {
        "eventName": "verification_started",
        "verificationId": "transition-2",
        "metadata": {"attempt_key": opaque},
    }
    with pytest.raises(ValueError, match="invalid identity verification transition"):
        process_metamap_event(db, raw_body=json.dumps(started).encode(), payload=started)
    with pytest.raises(ValueError, match="invalid identity provider metadata"):
        process_metamap_event(db, raw_body=b'{"eventName":"verification_started","metadata":[]}', payload={"eventName": "verification_started", "metadata": []})
