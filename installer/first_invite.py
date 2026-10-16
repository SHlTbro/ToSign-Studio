#!/usr/bin/env python3
"""First-invite bootstrap CLI (D03, B5 2026-09-27).

A fresh install has zero users and zero invites, and registration requires a
valid invite - without a bootstrap the packaged product can never create its
first (admin) account. This tool seeds ONE invite, and only while the users
table is still empty, so it is a first-run bootstrap and not a standing
invite generator (invite issuance for later users is an admin/owner flow).

The database is selected by YY_DB_URL exactly like the server process
(app.core.db); tables are created with the app's own metadata if missing.

Usage:
  python installer\\first_invite.py <code> [--uses N] [--yes]

Exit codes: 0 seeded (or already non-empty users: no-op); 1 error / refused.
"""
import argparse
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for _root in (_REPO,):
    if (_root / "app" / "main.py").is_file():
        sys.path.insert(0, str(_root))
        break


def parse_invite_code(value: str) -> str:
    """Match the registration page before the bootstrap can touch a database."""
    code = value.strip().upper()
    if re.fullmatch(r"[A-Z0-9-]{6,20}", code) is None:
        raise argparse.ArgumentTypeError("invite code must be 6-20 ASCII letters, digits or hyphens")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the first-run invite (empty users table only)")
    parser.add_argument("code", type=parse_invite_code, help="6-20 ASCII letters, digits or hyphens, e.g. FIRST-RUN")
    parser.add_argument("--uses", type=int, default=5, help="uses_total for the invite (default 5)")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    args = parser.parse_args()

    from app.core.db import Base, get_engine
    from app.domains.auth.models import Invite, User

    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    from sqlalchemy.orm import sessionmaker

    s = sessionmaker(bind=engine)()
    try:
        users = s.query(User).count()
        if users > 0:
            print(f"[first-invite] users table already has {users} account(s) - nothing seeded")
            return 0
        if not args.yes:
            answer = input(f"[first-invite] seed invite '{args.code}' (uses={args.uses})? y/N: ")
            if answer.strip().lower() != "y":
                print("[first-invite] aborted")
                return 1
        existing = s.query(Invite).filter(Invite.code == args.code).first()
        if existing is not None:
            existing.uses_total = max(int(existing.uses_total or 0), args.uses)
            existing.used_count = 0
            print(f"[first-invite] re-armed existing invite '{args.code}' (uses={args.uses})")
        else:
            s.add(Invite(code=args.code, uses_total=args.uses, used_count=0,
                         created_at="2026-09-27T00:00:00+00:00"))
            print(f"[first-invite] seeded invite '{args.code}' (uses={args.uses})")
        s.commit()
        print("[first-invite] next: register the first (admin) account via the app")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
