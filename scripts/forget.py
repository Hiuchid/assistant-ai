#!/usr/bin/env python3
"""Erase everything about one person, on request.

INSTRUCTIONS.md §13 asks for "a deletion path if someone asks". This is it.

    .venv/bin/python scripts/forget.py "dana@example.com"      # show only
    .venv/bin/python scripts/forget.py "03 456 789" --delete   # actually erase

**Dry run by default.** It prints what it would remove and stops. Deleting is
irreversible and cascades to the transcript and the item, so the destructive
form has to be asked for explicitly and then confirmed by typing the count.

The search is deliberately broad -- contact details, item titles and the raw
transcript -- because on an erasure request, missing a row is a worse failure
than showing one extra to a human who is about to confirm.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings
from backend.persistence import Store


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--delete"]
    destructive = "--delete" in sys.argv[1:]

    if len(args) != 1 or not args[0].strip():
        print(__doc__, file=sys.stderr)
        return 2
    needle = args[0].strip()

    settings = get_settings()
    if not settings.supabase_db_dsn:
        print("SUPABASE_DB_DSN is not set", file=sys.stderr)
        return 1

    store = Store(settings.supabase_db_dsn)
    await store.connect()
    try:
        matches = await store.find_conversations_for(needle)
        if not matches:
            print(f"Nothing matches {needle!r}.")
            return 0

        print(f"{len(matches)} conversation(s) match {needle!r}:\n")
        for m in matches:
            contact = m["contact"] or {}
            who = ", ".join(f"{k}={v}" for k, v in contact.items()) or "no contact recorded"
            print(f"  {m['id']}")
            print(f"    {m['started_at']:%Y-%m-%d %H:%M}  {m['mode']}/{m['channel']}")
            print(f"    {m['title'] or '(no item)'}")
            print(f"    {who}\n")

        if not destructive:
            print("Dry run. Re-run with --delete to erase these permanently.")
            return 0

        # Typing the count is deliberate: it cannot be satisfied by holding
        # down Enter, which "yes" can.
        print("This deletes the conversations, their transcripts and their items.")
        print("It cannot be undone.")
        answer = input(f"Type {len(matches)} to confirm: ").strip()
        if answer != str(len(matches)):
            print("Not confirmed; nothing deleted.")
            return 1

        deleted = await store.delete_conversations([m["id"] for m in matches])
        print(f"Deleted {deleted} conversation(s) and everything attached to them.")
    finally:
        await store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
