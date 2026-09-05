#!/usr/bin/env python3
"""Apply the retention policy. Run nightly from cron.

INSTRUCTIONS.md §13. The visitor widget tells people "transcripts are deleted
after 90 days", and this is the thing that makes that true rather than
aspirational. If you change the window, change the notice on message.html too.

Two stages, and only the first is on by default:

1. **Transcripts** past `RETENTION_TRANSCRIPT_DAYS`. Turns only -- the item
   built from them survives, so the record of who called and what they wanted
   is kept while the verbatim record of how they said it is not.
2. **Whole conversations** past `RETENTION_ITEM_DAYS`, cascading to their
   items. Disabled by default (0), because silently deleting the owner's own
   reminders is a worse failure than keeping them too long.

Prints what it did and exits non-zero on failure, so a silent cron failure
shows up in the log rather than looking like "nothing to delete".
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings
from backend.persistence import Store


async def main() -> int:
    settings = get_settings()
    if not settings.supabase_db_dsn:
        print("SUPABASE_DB_DSN is not set", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    store = Store(settings.supabase_db_dsn)
    await store.connect()
    try:
        turns = await store.purge_old_transcripts(settings.retention_transcript_days)
        print(
            f"{stamp} deleted {turns} turns older than "
            f"{settings.retention_transcript_days} days"
        )

        if settings.retention_item_days > 0:
            gone = await store.purge_old_conversations(settings.retention_item_days)
            print(
                f"{stamp} deleted {gone} conversations older than "
                f"{settings.retention_item_days} days"
            )
        else:
            print(f"{stamp} conversation purge disabled (RETENTION_ITEM_DAYS=0)")
    finally:
        await store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
