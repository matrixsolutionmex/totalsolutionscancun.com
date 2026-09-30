"""Plan or apply one explicitly guarded legacy User.role transition."""

import argparse
import json
import sys

from app.database.connection import SessionLocal
from app.models.user import User
from app.models.user_lifecycle import UserLifecycleEvent


ALLOWED_ROLES = {"ROOT", "GERENTE", "BROKER", "TECNICO"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual guarded user role transition")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="validate and roll back")
    mode.add_argument("--apply", action="store_true", help="apply the transition")
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--new-role", required=True)
    parser.add_argument("--expected-organization-id", required=True, type=int)
    parser.add_argument("--actor-user-id", required=True, type=int)
    return parser


def plan_role_transition(db, *, user_id: int, expected_role: str, new_role: str,
                         expected_organization_id: int, actor_user_id: int):
    """Lock and validate a role transition without changing persistent state."""
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    actor = db.query(User).filter(User.id == actor_user_id).with_for_update().first()
    if user is None or actor is None:
        raise ValueError("user or actor not found")
    if actor.role != "ROOT":
        raise ValueError("actor must be ROOT")
    if user.organization_id != expected_organization_id:
        raise ValueError("organization expectation mismatch")
    current_role = (user.role or "").upper()
    if current_role != new_role and current_role != expected_role:
        raise ValueError("current role expectation mismatch")
    return {
        "user": user,
        "actor": actor,
        "expected_role": expected_role,
        "new_role": new_role,
        "current_role": current_role,
        "expected_organization_id": expected_organization_id,
    }


def apply_role_transition(db, plan: dict) -> bool:
    """Apply a previously validated plan; caller owns commit/rollback."""
    if plan["current_role"] == plan["new_role"]:
        return False
    user = plan["user"]
    user.role = plan["new_role"]
    db.add(UserLifecycleEvent(
        organization_id=user.organization_id,
        user_id=user.id,
        actor_user_id=plan["actor"].id,
        event_type="ROLE_TRANSITION",
        from_status=plan["current_role"],
        to_status=plan["new_role"],
        reason="NETWORK-01 manual guarded role transition",
        metadata_json=json.dumps({
            "expected_role": plan["expected_role"],
            "expected_organization_id": plan["expected_organization_id"],
        }, sort_keys=True),
    ))
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected_role = args.expected_role.strip().upper()
    new_role = args.new_role.strip().upper()
    if expected_role not in ALLOWED_ROLES or new_role not in ALLOWED_ROLES:
        print("role_transition=BLOCKED reason=unsupported_role", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        plan = plan_role_transition(
            db,
            user_id=args.user_id,
            expected_role=expected_role,
            new_role=new_role,
            expected_organization_id=args.expected_organization_id,
            actor_user_id=args.actor_user_id,
        )
        current_role = plan["current_role"]
        if current_role == new_role:
            db.rollback()
            print("role_transition=PASS action=UNCHANGED writes=0")
            return 0
        user = plan["user"]
        print(f"planned_user={user.id} from_role={current_role} to_role={new_role} organization_id={user.organization_id}")
        if args.dry_run:
            db.rollback()
            print("dry_run=PASS writes=0")
            return 0

        apply_role_transition(db, plan)
        db.commit()
        print("role_transition=PASS writes=1 audit=1")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"role_transition=BLOCKED reason={exc}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
