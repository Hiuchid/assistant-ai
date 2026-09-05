#!/usr/bin/env bash
# Keep the Supabase free-tier project awake.
#
# INSTRUCTIONS.md §8: free projects pause after 7 days of inactivity, where
# "activity" means real database queries -- not dashboard visits, and not API
# calls that never reach Postgres. The backend's connection pool would normally
# provide that, but a quiet week with no conversations produces no queries at
# all, which is exactly when the project would pause and the next caller would
# hit a cold, unreachable database.
#
# Runs every 6 hours from cron.
set -euo pipefail

LOG=/var/log/supabase-keepalive.log
cd /root/assistant-ai

DSN=$(grep '^SUPABASE_DB_DSN=' .env | cut -d= -f2-)
if [[ -z "$DSN" ]]; then
    echo "$(date -Is) no DSN configured" >> "$LOG"
    exit 1
fi

# A trivial query is enough; the point is that Postgres serves something.
if psql "$DSN" -tAc "select 'awake', now();" >> "$LOG" 2>&1; then
    echo "$(date -Is) ok" >> "$LOG"
else
    echo "$(date -Is) FAILED" >> "$LOG"
    exit 1
fi

# Keep the log from growing without bound.
tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
