#!/usr/bin/env bash
# Deploy to the VPS. INSTRUCTIONS.md Phase 0 requires a documented deploy path.
#
#   ./deploy.sh            sync code, install deps, restart
#   ./deploy.sh --no-deps  skip pip install (faster for code-only changes)
#
# Deliberately not a git push: the repo is not on the VPS and does not need to
# be. This syncs the working tree over SSH and restarts the unit.

set -euo pipefail

HOST="${ASSISTANT_HOST:-root@109.199.116.38}"
KEY="${ASSISTANT_KEY:-$HOME/.ssh/assistant_ai}"
REMOTE="/root/assistant-ai"
SSH=(ssh -i "$KEY" -o BatchMode=yes "$HOST")

install_deps=1
[[ "${1:-}" == "--no-deps" ]] && install_deps=0

echo "==> syncing code to $HOST:$REMOTE"
# .env lives only on the server and must never be overwritten from here.
tar czf - \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.env' \
    --exclude='audio_cache' \
    backend infra requirements.txt INSTRUCTIONS.md \
  | "${SSH[@]}" "mkdir -p $REMOTE && tar xzf - -C $REMOTE"

if [[ $install_deps -eq 1 ]]; then
  echo "==> installing dependencies"
  "${SSH[@]}" "cd $REMOTE && .venv/bin/pip install -q -r requirements.txt"
fi

echo "==> restarting service"
"${SSH[@]}" "systemctl restart assistant-ai && sleep 1 && systemctl is-active assistant-ai"

echo "==> health check"
"${SSH[@]}" "curl -fsS http://127.0.0.1:8000/health" && echo ""
echo "==> done"
