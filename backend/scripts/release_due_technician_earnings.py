"""Controlled batch release of technician earnings after warranty expiry."""

import argparse
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Register the complete model graph without invoking FastAPI startup.
import app.main  # noqa: F401
from app.services.technician_earning_reconciliation_service import release_due_technician_earnings


def _utc_argument(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="plan only and roll back")
    parser.add_argument("--apply", action="store_true", help="apply one controlled batch")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--now", help="timezone-aware UTC timestamp, for deterministic tests")
    args = parser.parse_args()
    if args.dry_run == args.apply:
        parser.error("choose exactly one of --dry-run or --apply")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    try:
        now = _utc_argument(args.now)
    except ValueError as exc:
        parser.error(str(exc))
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    engine = create_engine(url)
    session = sessionmaker(bind=engine)()
    try:
        if args.dry_run:
            session.execute(text("SET TRANSACTION READ ONLY"))
        result = release_due_technician_earnings(
            session, now, args.batch_size, dry_run=args.dry_run,
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
        print({"mode": "dry-run" if args.dry_run else "apply", **result})
        return 0
    except Exception as exc:
        session.rollback()
        print(f"release_due_technician_earnings failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
