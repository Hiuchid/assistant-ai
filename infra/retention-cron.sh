#!/usr/bin/env bash
# Nightly retention pass (§13). Wrapper so cron gets the venv and the cwd.
set -euo pipefail
cd /root/assistant-ai
LOG=/var/log/assistant-retention.log
.venv/bin/python scripts/retention.py >> "$LOG" 2>&1
tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
