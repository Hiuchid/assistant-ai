#!/usr/bin/env python3
"""Create or update an operator account.

Run on the VPS:

    cd /root/assistant-ai && .venv/bin/python scripts/create_operator.py

The password is read with getpass, so it is never echoed, never passed as an
argument (which would put it in the shell history and the process list), and
never written anywhere except as a scrypt hash. Nobody but you ever sees it --
including whoever set this up.
"""

from __future__ import annotations

import asyncio
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from backend.auth import hash_password
from backend.config import get_settings

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_LENGTH = 12


async def main() -> int:
    settings = get_settings()
    if not settings.supabase_db_dsn:
        print("SUPABASE_DB_DSN is not set in .env", file=sys.stderr)
        return 1

    email = input("email: ").strip().lower()
    if not EMAIL_RE.match(email):
        print("that does not look like an email address", file=sys.stderr)
        return 1

    role = (input("role [operator/owner] (default owner): ").strip() or "owner").lower()
    if role not in ("operator", "owner"):
        print("role must be 'operator' or 'owner'", file=sys.stderr)
        return 1

    password = getpass.getpass("password: ")
    # Length is the only rule. Composition rules push people toward
    # "Password1!" and buy nothing; length is what actually costs an attacker.
    if len(password) < MIN_LENGTH:
        print(f"password must be at least {MIN_LENGTH} characters", file=sys.stderr)
        return 1
    if password != getpass.getpass("password again: "):
        print("passwords did not match", file=sys.stderr)
        return 1

    stored = hash_password(password)
    del password

    conn = await asyncpg.connect(settings.supabase_db_dsn)
    try:
        row = await conn.fetchrow(
            """
            insert into users (email, password_hash, role)
            values ($1, $2, $3)
            on conflict (email) do update
                set password_hash = excluded.password_hash,
                    role          = excluded.role,
                    disabled      = false
            returning id::text, role, (xmax = 0) as created
            """,
            email, stored, role,
        )
    finally:
        await conn.close()

    if row is None:
        print("insert returned nothing", file=sys.stderr)
        return 1
    action = "created" if row["created"] else "updated"
    print(f"{action} {email} as {row['role']} (id {row['id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
