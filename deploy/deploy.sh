#!/bin/bash
# One-command update for the Inboxx verifier on the server.
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

# The `git reset --hard` below rewrites THIS FILE while bash is still reading
# it, and bash reads a script lazily by byte offset -- so once deploy.sh itself
# starts changing between releases, an update can leave bash executing whatever
# happens to sit at the offset it had reached. Harmless while the file never
# changed; a corrupt half-deploy the first time it does.
#
# Re-exec from a private copy, so the file on disk is free to change under us.
if [ -z "${DEPLOY_FROM_COPY:-}" ]; then
  _copy="$(mktemp /tmp/deploy.XXXXXX.sh)"
  cat "$0" > "$_copy"
  trap 'rm -f "$_copy"' EXIT
  DEPLOY_FROM_COPY=1 bash "$_copy" "$@"
  exit $?
fi

APP_DIR="${APP_DIR:-/opt/email-verifier}"
REPO="${REPO:-https://github.com/aroraayan01/Email-Verification.git}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-email-verifier}"

echo "== Inboxx deploy =="
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
else
  echo "== fetching latest =="
  git remote set-url origin "$REPO"
  git fetch --depth 1 origin "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

# Normalise the branch on EVERY run, not just the first.
#
# `git init` starts on whatever the local default is -- usually `master` -- and
# `git branch -M` can fail quietly there on older git, leaving a branch that is
# neither named after the remote nor tracking it. deploy.sh itself doesn't care
# (it fetches and resets by full ref), but a plain `git pull` then fails with
# "no tracking information", which is a confusing thing to hit by hand. Fixing
# it here means an already-broken checkout heals the next time this runs.
CURRENT="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
if [ "$CURRENT" != "$BRANCH" ]; then
  echo "== renaming branch '$CURRENT' -> '$BRANCH' =="
  git branch -f "$BRANCH" "origin/$BRANCH"
  git symbolic-ref HEAD "refs/heads/$BRANCH"
  git reset --hard "origin/$BRANCH"
  # Drop the stale default branch, but never the one we just moved onto.
  [ "$CURRENT" != "HEAD" ] && [ "$CURRENT" != "$BRANCH" ] && \
    git branch -D "$CURRENT" >/dev/null 2>&1 || true
fi
git branch --set-upstream-to "origin/$BRANCH" "$BRANCH" >/dev/null 2>&1 || true

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
