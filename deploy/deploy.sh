#!/bin/bash
# One-command update for the Xomexo verifier on the server.
#
#     bash /opt/email-verifier/deploy/deploy.sh
#
# Pulls the latest code from GitHub, refreshes dependencies, and restarts the
# service. Your data and secrets are never touched: app.env, webdata/ and the
# .venv are untracked, so a hard reset to origin/main leaves them exactly as
# they are.
#
# The first run bootstraps: if /opt/email-verifier is still the old tarball
# unpack (no .git), it adopts the directory as a git checkout in place --
# again without deleting app.env or webdata.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/email-verifier}"
REPO="${REPO:-https://github.com/aroraayan01/Email-Verification.git}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-email-verifier}"

echo "== Xomexo deploy =="
echo "   dir:    $APP_DIR"
echo "   repo:   $REPO ($BRANCH)"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

# --- 1. make sure this directory is a git checkout of the repo --------------
if [ ! -d .git ]; then
  echo "== first run: adopting $APP_DIR as a git checkout (data preserved) =="
  git init -q
  git remote add origin "$REPO"
  git fetch --depth 1 origin "$BRANCH"
  # Point HEAD at the remote branch and overwrite ONLY tracked files.
  # app.env, webdata/, .venv/ are untracked and stay put.
  git reset --hard "origin/$BRANCH"
  git branch -q -M "$BRANCH" 2>/dev/null || true
  git branch -q --set-upstream-to "origin/$BRANCH" "$BRANCH" 2>/dev/null || true
else
  echo "== fetching latest =="
  git remote set-url origin "$REPO"
  git fetch --depth 1 origin "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

echo "== now at: $(git log --oneline -1) =="

# --- 2. refresh dependencies (cheap no-op when nothing changed) -------------
export PATH="/usr/local/bin:/root/.local/bin:$PATH"
if [ -x "$APP_DIR/.venv/bin/python" ] && command -v uv >/dev/null 2>&1; then
  echo "== syncing dependencies =="
  VIRTUAL_ENV="$APP_DIR/.venv" uv pip install -q -r requirements.txt
else
  echo "!! no .venv yet -- run deploy/install_app.sh first for the initial setup"
  exit 1
fi

# --- 3. restart and verify --------------------------------------------------
echo "== restarting $SERVICE =="
systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  echo "==================================================================="
  echo "  DEPLOYED — $SERVICE is running $(git rev-parse --short HEAD)"
  echo "==================================================================="
else
  echo "!! $SERVICE failed to start -- last logs:"
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  exit 1
fi
