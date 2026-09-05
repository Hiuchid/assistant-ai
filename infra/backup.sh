#!/usr/bin/env bash
# Nightly pg_dump to local disk, 7-day retention.
#
# INSTRUCTIONS.md §8: **the Supabase free tier has no backups.** Not "backups
# you have to restore manually" -- none at all. If the project is deleted, or a
# migration goes wrong, everything is gone. This is the only copy.
#
# Dumps to the VPS rather than anywhere clever, because the VPS has 68 GB free
# and the whole database is measured in megabytes. Note the obvious limitation:
# a single machine holding both the service and its only backup is one failure
# away from losing both. Worth moving off-box before this holds anything the
# operator would miss.
set -euo pipefail

BACKUP_DIR=/root/backups
RETENTION_DAYS=7
LOG=/var/log/assistant-backup.log
cd /root/assistant-ai

mkdir -p "$BACKUP_DIR"
DSN=$(grep '^SUPABASE_DB_DSN=' .env | cut -d= -f2-)
if [[ -z "$DSN" ]]; then
    echo "$(date -Is) no DSN configured" >> "$LOG"
    exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/assistant-$STAMP.sql.gz"

# --no-owner and --no-acl so the dump restores into any database, not just one
# with Supabase's exact role set.
if pg_dump "$DSN" --no-owner --no-acl --schema=public 2>>"$LOG" | gzip > "$OUT"; then
    SIZE=$(stat -c%s "$OUT")
    if [[ "$SIZE" -lt 1000 ]]; then
        # A dump that succeeds but produces nothing is worse than a failure,
        # because it looks fine in a directory listing.
        echo "$(date -Is) SUSPICIOUS: dump only $SIZE bytes" >> "$LOG"
        exit 1
    fi
    echo "$(date -Is) ok $OUT ($SIZE bytes)" >> "$LOG"
else
    echo "$(date -Is) FAILED" >> "$LOG"
    rm -f "$OUT"
    exit 1
fi

find "$BACKUP_DIR" -name 'assistant-*.sql.gz' -mtime "+$RETENTION_DAYS" -delete
tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
