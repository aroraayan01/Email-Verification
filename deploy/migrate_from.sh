#!/bin/bash
# Pull this app's DATA from another server that is already running it.
#
#     bash deploy/migrate_from.sh root@134.195.138.179
#     bash deploy/migrate_from.sh root@old.example.com /opt/email-verifier
#
# Run this ON THE NEW SERVER, after deploy/install_app.sh has created the
# service here. Code comes from git; this moves only what git cannot:
#
#   webdata/users.sqlite3     accounts, API keys, quotas, activity log
#   webdata/verdicts.sqlite3  the verdict cache -- INCLUDING every Clearout
#                             verdict already paid for. Losing this means
#                             buying those addresses a second time.
#   webdata/jobs.sqlite3      bulk job records
#   webdata/results/          the CSVs those jobs produced
#
# app.env is deliberately NOT copied: it carries the old host's SMTP_HELO and
# BASE_URL, which are wrong here. Copy the two values you actually want by
# hand (APP_SECRET to keep sessions valid, CLEAROUT_API_KEY) -- the script
# prints them at the end so you can paste them.
#
# The service is stopped for the copy and restarted after, so nothing is
# written underneath a database mid-transfer.
set -euo pipefail

REMOTE="${1:-}"
REMOTE_DIR="${2:-/opt/email-verifier}"
APP_DIR="${APP_DIR:-/opt/email-verifier}"
SERVICE="${SERVICE:-email-verifier}"

if [ -z "$REMOTE" ]; then
  echo "usage: bash deploy/migrate_from.sh user@old-server [remote-app-dir]" >&2
  exit 2
fi

echo "== migrating data from $REMOTE:$REMOTE_DIR =="
echo "   into $APP_DIR"

command -v rsync >/dev/null 2>&1 || { echo "!! rsync not found -- yum install -y rsync" >&2; exit 1; }

# --- 1. sanity: can we reach it, and is the data actually there? ------------
if ! ssh -o BatchMode=no -o ConnectTimeout=15 "$REMOTE" "test -f '$REMOTE_DIR/webdata/users.sqlite3'"; then
  echo "!! $REMOTE_DIR/webdata/users.sqlite3 not found on $REMOTE" >&2
  echo "   check the path, or pass it as the second argument" >&2
  exit 1
fi

# --- 2. back up whatever is here now ---------------------------------------
if [ -d "$APP_DIR/webdata" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  cp -a "$APP_DIR/webdata" "$APP_DIR/webdata.bak-$STAMP"
  echo "== existing webdata backed up to webdata.bak-$STAMP =="
fi

# --- 3. stop the service so nothing writes mid-copy ------------------------
WAS_RUNNING=0
if systemctl is-active --quiet "$SERVICE"; then
  WAS_RUNNING=1
  systemctl stop "$SERVICE"
  echo "== stopped $SERVICE for the copy =="
fi

# --- 4. copy ---------------------------------------------------------------
mkdir -p "$APP_DIR/webdata"
rsync -az --info=stats2 "$REMOTE:$REMOTE_DIR/webdata/" "$APP_DIR/webdata/"

echo "== copied: =="
for f in users.sqlite3 verdicts.sqlite3 jobs.sqlite3; do
  if [ -f "$APP_DIR/webdata/$f" ]; then
    echo "   $f  $(du -h "$APP_DIR/webdata/$f" | cut -f1)"
  fi
done
[ -d "$APP_DIR/webdata/results" ] && \
  echo "   results/  $(find "$APP_DIR/webdata/results" -type f | wc -l) file(s)"

# --- 5. what did we actually get? ------------------------------------------
PY="$APP_DIR/.venv/bin/python"
if [ -x "$PY" ]; then
  "$PY" - "$APP_DIR/webdata" <<'PYEOF'
import sqlite3, sys, os
base = sys.argv[1]
u = os.path.join(base, "users.sqlite3")
v = os.path.join(base, "verdicts.sqlite3")
if os.path.exists(u):
    c = sqlite3.connect(u)
    n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print("   accounts carried over: %d" % n)
    for r in c.execute("SELECT email, is_admin FROM users ORDER BY id"):
        print("     - %s%s" % (r[0], "  (admin)" if r[1] else ""))
if os.path.exists(v):
    c = sqlite3.connect(v)
    total = c.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
    paid = c.execute("SELECT COUNT(*) FROM verdicts WHERE source='clearout'").fetchone()[0]
    print("   cached verdicts: %d (%d bought from Clearout -- not re-buying those)"
          % (total, paid))
PYEOF
fi

# --- 6. the two secrets worth carrying across ------------------------------
echo
echo "== from the old app.env -- paste these into $APP_DIR/app.env =="
echo "   (APP_SECRET keeps existing login sessions valid; without it everyone"
echo "    is simply logged out once, which is harmless.)"
ssh "$REMOTE" "grep -E '^(APP_SECRET|CLEAROUT_API_KEY|APP_PASSWORD)=' '$REMOTE_DIR/app.env' || true"

# --- 7. restart ------------------------------------------------------------
if [ "$WAS_RUNNING" -eq 1 ]; then
  systemctl start "$SERVICE"
  sleep 2
  systemctl is-active --quiet "$SERVICE" && echo "== $SERVICE running ==" \
    || { echo "!! failed to start:"; journalctl -u "$SERVICE" -n 20 --no-pager; }
fi

echo
echo "==================================================================="
echo "  Data migrated. Still to do on this server:"
echo "    1. paste the secrets above into $APP_DIR/app.env"
echo "    2. systemctl restart $SERVICE"
echo "    3. bash deploy/setup_proxy.sh <your-domain>"
echo "  Leave the old server running until this one is confirmed working."
echo "==================================================================="
