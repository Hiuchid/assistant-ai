#!/usr/bin/env bash
# Deploy to the VPS. INSTRUCTIONS.md Phase 0 requires a documented deploy path.
#
#   ./deploy.sh            sync code, install deps, restart
#   ./deploy.sh --no-deps  skip pip install (faster for code-only changes)
#
# Deliberately not a git push: the repo is not on the VPS and does not need to
# be. This syncs the working tree over SSH and restarts the unit.

set -euo pipefail

# Not baked in: this repo is public. Set it in your shell or a local
# untracked file, e.g. export ASSISTANT_HOST=root@203.0.113.10
HOST="${ASSISTANT_HOST:?set ASSISTANT_HOST, e.g. root@your.vps.example}"
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

# Startup warms the TTS cache before it binds, which takes a few seconds, so a
# single immediate curl reports a connection failure on every healthy deploy.
echo "==> health check"
"${SSH[@]}" 'for i in $(seq 20); do
  curl -fs http://127.0.0.1:8000/health && exit 0
  sleep 1
done
curl -fsS http://127.0.0.1:8000/health' && echo ""
echo "==> done"
