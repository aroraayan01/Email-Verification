#!/bin/bash
# Installs the Email Verifier as an isolated service on this server.
# Nothing here touches the system Python or any existing site: a self-contained
# Python 3.11 is fetched by `uv` into this app's own folder, and the service
# binds to localhost only (Apache will front it).
#
# Run as root from the extracted app directory:
#     cd /opt/email-verifier && bash deploy/install_app.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$APP_DIR/app.env"
SERVICE=/etc/systemd/system/email-verifier.service
PORT="${PORT:-8000}"

echo "== app dir: $APP_DIR =="

# --- 1. uv (single static binary; installs its own Python) -----------------
if ! command -v uv >/dev/null 2>&1; then
  export UV_INSTALL_DIR=/usr/local/bin
  echo "== installing uv =="
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="/usr/local/bin:/root/.local/bin:$PATH"
UV="$(command -v uv)"
echo "== uv: $UV =="

# --- 2. isolated Python 3.11 + venv + deps ---------------------------------
cd "$APP_DIR"
echo "== creating venv with Python 3.11 (uv fetches it if missing) =="
"$UV" venv --python 3.11 .venv
echo "== installing dependencies =="
VIRTUAL_ENV="$APP_DIR/.venv" "$UV" pip install -r requirements.txt

# --- 3. environment file (password + secret) -------------------------------
if [ ! -f "$ENV_FILE" ]; then
  SECRET="$(head -c32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c40)"
  if [ -n "${APP_PASSWORD:-}" ]; then
    PW="$APP_PASSWORD"
  else
    PW="$(head -c18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c14)"
  fi
  cat > "$ENV_FILE" <<EOF
APP_PASSWORD=$PW
APP_SECRET=$SECRET
ENABLE_SMTP=1
SMTP_HELO=srv.tradegeniusglobal.com
SMTP_MAIL_FROM=
USE_CACHE=0
PORT=$PORT
EOF
  chmod 600 "$ENV_FILE"
  echo "== wrote $ENV_FILE =="
  GENERATED_PW="$PW"
else
  echo "== keeping existing $ENV_FILE (password unchanged) =="
  GENERATED_PW=""
fi

# --- 4. systemd service ----------------------------------------------------
cat > "$SERVICE" <<EOF
[Unit]
Description=Email Verifier
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn webapp.main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable email-verifier >/dev/null 2>&1 || true
systemctl restart email-verifier
sleep 3

echo
echo "==================================================================="
if systemctl is-active --quiet email-verifier; then
  echo "  Email Verifier is RUNNING on 127.0.0.1:$PORT"
else
  echo "  SERVICE FAILED TO START -- check: journalctl -u email-verifier -n 40"
fi
if [ -n "$GENERATED_PW" ]; then
  echo "  Login password:  $GENERATED_PW"
  echo "  (change later by editing $ENV_FILE then: systemctl restart email-verifier)"
fi
echo "  Next: bash deploy/setup_proxy.sh   to point xomexo.com at it"
echo "==================================================================="
