"""Dry-run-first reconciliation for an accidental Google signup.

This command deliberately requires --apply for writes. It is intended to run
through the approved production database tunnel, never with a copied URL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import func, inspect, select

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.database.connection import SessionLocal  # noqa: E402
from app.models.auth_security import UserIdentity  # noqa: E402
from app.models.commercial_subscription import CommercialSubscription  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.organization_marketplace_link import OrganizationMarketplaceLink  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_commercial_profile import UserCommercialProfile  # noqa: E402
from app.services.user_lifecycle_service import transition_user_status  # noqa: E402

# Importing the application registers every model in Base.metadata.
from app import main as _app_model_registry  # noqa: F401,E402
from app.database.connection import Base  # noqa: E402


CANONICAL_USER_ID = 1
ACCIDENTAL_USER_ID = 38
CANONICAL_ORGANIZATION_ID = 1
ACCIDENTAL_ORGANIZATION_ID = 9


def _fk_counts(session, *, table_id: str, value: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    inspector = inspect(session.bind)
    for table in Base.metadata.sorted_tables:
        if table.name == "users":
            continue
        if not inspector.has_table(table.name):
            continue
        for column in table.columns:
            if any(
                fk.column.table.name == table_id and fk.column.name == "id"
                for fk in column.foreign_keys
            ):
                counts[f"{table.name}.{column.name}"] = int(
                    session.execute(select(func.count()).select_from(table).where(column == value)).scalar_one()
                )
    return counts


def _organization_counts(session, organization_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    inspector = inspect(session.bind)
    for table in Base.metadata.sorted_tables:
        if table.name == "organizations" or not inspector.has_table(table.name):
            continue
        column = table.columns.get("organization_id")
        if column is not None:
            counts[f"{table.name}.organization_id"] = int(
                session.execute(select(func.count()).select_from(table).where(column == organization_id)).scalar_one()
            )
    return counts


def audit(session) -> dict:
    canonical = session.get(User, CANONICAL_USER_ID)
    accidental = session.get(User, ACCIDENTAL_USER_ID)
    organization = session.get(Organization, ACCIDENTAL_ORGANIZATION_ID)
    subscription = session.query(CommercialSubscription).filter_by(organization_id=ACCIDENTAL_ORGANIZATION_ID).first()
    commercial_profile = session.query(UserCommercialProfile).filter_by(user_id=ACCIDENTAL_USER_ID).first()
    marketplace_links = session.query(OrganizationMarketplaceLink).filter_by(
        organization_id=ACCIDENTAL_ORGANIZATION_ID,
    ).order_by(OrganizationMarketplaceLink.id.asc()).all()
    identities = session.query(UserIdentity).filter(UserIdentity.user_id.in_([CANONICAL_USER_ID, ACCIDENTAL_USER_ID])).all()
    user_counts = _fk_counts(session, table_id="users", value=ACCIDENTAL_USER_ID)
    organization_counts = _organization_counts(session, ACCIDENTAL_ORGANIZATION_ID)
    operational_keys = {
        "service_orders", "service_order_financials", "service_order_ledger_entries",
        "platform_ledger_entries", "payments", "service_order_quotes", "service_order_diagnoses",
        "service_order_warranty_claims", "service_order_tracking", "service_order_reviews",
        "technician_skills", "leads", "contracts", "service_opportunities", "service_requests",
        "service_properties", "service_order_payment_plans", "service_order_payment_installments",
    }
    return {
        "canonical_user_id": getattr(canonical, "id", None),
        "canonical_user_role": getattr(canonical, "role", None),
        "canonical_user_status": getattr(canonical, "status", None),
        "canonical_organization_id": getattr(canonical, "organization_id", None),
        "accidental_user_id": getattr(accidental, "id", None),
        "accidental_user_status": getattr(accidental, "status", None),
        "accidental_user_active": bool(getattr(accidental, "is_active", False)),
        "accidental_user_organization_id": getattr(accidental, "organization_id", None),
        "accidental_user_commercial_profile": {
            "exists": commercial_profile is not None,
            "plan": getattr(commercial_profile, "plan", None),
            "status": getattr(commercial_profile, "status", None),
            "source": getattr(commercial_profile, "source", None),
        },
        "accidental_organization_status": getattr(organization, "status", None),
        "accidental_organization_is_platform_owner": bool(getattr(organization, "is_platform_owner", False)),
        "accidental_organization_user_count": session.query(User).filter(User.organization_id == ACCIDENTAL_ORGANIZATION_ID).count(),
        "marketplace_links": [
            {
                "id": link.id,
                "slug": link.slug,
                "source_code": link.source_code,
                "visibility_scope": link.visibility_scope,
                "active": bool(link.active),
                "service_category_present": bool(link.service_category),
                "campaign_name_present": bool(link.campaign_name),
            }
            for link in marketplace_links
        ],
        "subscription": {
            "exists": subscription is not None,
            "plan": getattr(subscription, "plan", None),
            "status": getattr(subscription, "status", None),
            "provider": getattr(subscription, "provider", None),
            "external_reference_present": bool(getattr(subscription, "external_reference", None)),
        },
        "google_identity_counts": {
            str(user_id): sum(1 for identity in identities if identity.user_id == user_id and identity.provider == "google")
            for user_id in [CANONICAL_USER_ID, ACCIDENTAL_USER_ID]
        },
        "user38_fk_counts": user_counts,
        "organization9_counts": organization_counts,
        "operational_user38_count": sum(count for key, count in user_counts.items() if key.split(".", 1)[0] in operational_keys),
        "operational_organization9_count": sum(count for key, count in organization_counts.items() if key.split(".", 1)[0] in operational_keys),
        "action_plan": {
            "user_38": "ARCHIVE via user lifecycle; revoke access; preserve history",
            "organization_9": "TRANSITION to ORPHANED_ONBOARDING via existing residual-workspace status",
            "subscription_9": "CANCEL residual FREE/MOCK subscription; preserve record",
            "marketplace_links": "DEACTIVATE bootstrap links with active=false; never delete",
            "commercial_profile_38": "PRESERVE historical profile; do not delete",
            "audit_and_lifecycle": "PRESERVE existing records; do not rewrite history",
        },
    }


def validate_apply_preconditions(report: dict) -> None:
    expected = {
        "canonical_user_id": CANONICAL_USER_ID,
        "canonical_user_role": "ROOT",
        "canonical_user_status": "ACTIVE",
        "canonical_organization_id": CANONICAL_ORGANIZATION_ID,
        "accidental_user_id": ACCIDENTAL_USER_ID,
        "accidental_user_organization_id": ACCIDENTAL_ORGANIZATION_ID,
    }
    mismatches = {key: (value, report.get(key)) for key, value in expected.items() if report.get(key) != value}
    if mismatches:
        raise RuntimeError(f"Preconditions failed: {mismatches}")
    if report["canonical_organization_id"] == ACCIDENTAL_ORGANIZATION_ID:
        raise RuntimeError("Refusing apply against the canonical organization")
    if report["accidental_organization_is_platform_owner"]:
        raise RuntimeError("Refusing apply against a platform-owner organization")
    if report["accidental_organization_user_count"] != 1:
        raise RuntimeError("Refusing apply unless the residual organization has exactly one user")
    if report["accidental_user_organization_id"] != ACCIDENTAL_ORGANIZATION_ID:
        raise RuntimeError("Refusing apply when the accidental user is not the sole residual tenant user")
    if report["accidental_organization_status"] not in {"ACTIVE", "PENDING_ONBOARDING", "ORPHANED_ONBOARDING"}:
        raise RuntimeError("Refusing apply for an unsupported organization status")
    if report["google_identity_counts"][str(ACCIDENTAL_USER_ID)] != 0:
        raise RuntimeError("Refusing apply while the accidental user owns a Google identity")
    if report["operational_user38_count"] or report["operational_organization9_count"]:
        raise RuntimeError("Refusing apply while operational references exist")
    links = report["marketplace_links"]
    if len(links) != 1 or any(
        link["slug"] != "default"
        or link["source_code"] != "MARKETPLACE_LINK"
        or link["visibility_scope"] != "ORGANIZATION"
        or link["service_category_present"]
        or link["campaign_name_present"]
        for link in links
    ):
        raise RuntimeError("Refusing apply unless the marketplace link is the untouched bootstrap link")
    subscription = report["subscription"]
    if subscription["exists"] and (
        subscription["plan"] != "FREE"
        or subscription["status"] not in {"LAUNCH_ACCESS", "CANCELLED"}
        or subscription["provider"] != "MOCK"
        or subscription["external_reference_present"]
    ):
        raise RuntimeError("Refusing apply for a non-residual subscription")


def apply_reconciliation(session, report: dict) -> None:
    validate_apply_preconditions(report)
    try:
        accidental = session.get(User, ACCIDENTAL_USER_ID)
        actor = session.get(User, CANONICAL_USER_ID)
        if accidental.status != "ARCHIVED":
            transition_user_status(
                session,
                user=accidental,
                actor=actor,
                to_status="ARCHIVED",
                reason="Cadastro Google acidental reconciliado com o usuário canônico 1",
                event_type="ARCHIVED_DUPLICATE_GOOGLE_SIGNUP",
                is_active=False,
                metadata={"canonical_user_id": CANONICAL_USER_ID},
            )
        organization = session.get(Organization, ACCIDENTAL_ORGANIZATION_ID)
        if organization and organization.status in {"ACTIVE", "PENDING_ONBOARDING"}:
            organization.status = "ORPHANED_ONBOARDING"
        for link in session.query(OrganizationMarketplaceLink).filter_by(
            organization_id=ACCIDENTAL_ORGANIZATION_ID,
        ).all():
            if link.active:
                link.active = False
        subscription = session.query(CommercialSubscription).filter_by(organization_id=ACCIDENTAL_ORGANIZATION_ID).first()
        if subscription and subscription.status != "CANCELLED":
            subscription.status = "CANCELLED"
        session.commit()
    except Exception:
        session.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile an accidental Google signup; dry-run is the default.")
    parser.add_argument("--apply", action="store_true", help="Apply only after all preconditions pass.")
    args = parser.parse_args()
    with SessionLocal() as session:
        report = audit(session)
        if args.apply:
            apply_reconciliation(session, report)
            report = audit(session)
        else:
            # Keep the default mode read-only even if the session is reused by a driver.
            session.rollback()
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
