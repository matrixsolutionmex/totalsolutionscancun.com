"""Plan or apply the manual NETWORK-01 membership backfill."""

import argparse
import sys

from app.database.connection import SessionLocal
from app.services.organization_membership_service import (
    apply_membership_backfill,
    plan_membership_backfill,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual organization membership backfill")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="plan changes and roll back")
    mode.add_argument("--apply", action="store_true", help="apply the planned changes")
    parser.add_argument("--owner-user-id", action="append", type=int, default=[], help="explicit OWNER user id; repeatable")
    parser.add_argument("--admin-user-id", action="append", type=int, default=[], help="explicit ADMIN user id; repeatable")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = SessionLocal()
    try:
        plan = plan_membership_backfill(
            db,
            owner_user_ids=set(args.owner_user_id),
            admin_user_ids=set(args.admin_user_id),
        )
        actions = {action: sum(item["action"] == action for item in plan) for action in ("CREATE", "UPDATE", "UNCHANGED")}
        print(f"planned_users={len(plan)} create={actions['CREATE']} update={actions['UPDATE']} unchanged={actions['UNCHANGED']}")
        if args.dry_run:
            db.rollback()
            print("dry_run=PASS writes=0")
            return 0
        counts = apply_membership_backfill(db, plan)
        db.commit()
        print(f"apply=PASS created={counts['created']} updated={counts['updated']} unchanged={counts['unchanged']}")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"backfill=BLOCKED reason={exc}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
