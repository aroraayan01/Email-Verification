#!/bin/bash
# Points xomexo.com at the local Email Verifier service via Apache reverse
# proxy, using cPanel's per-domain include mechanism. This only adds config
# for xomexo.com -- no other domain or vhost is touched.
#
#     bash deploy/setup_proxy.sh
set -euo pipefail

USER=grapme
DOMAIN=xomexo.com
PORT="${PORT:-8000}"

echo "== wiring $DOMAIN -> 127.0.0.1:$PORT =="

# The proxy block. /.well-known is excluded so AutoSSL HTTP validation and
# certificate renewals keep working.
read -r -d '' PROXY <<EOF || true
ProxyPreserveHost On
ProxyPass /.well-known !
ProxyPass / http://127.0.0.1:$PORT/
ProxyPassReverse / http://127.0.0.1:$PORT/
RequestHeader set X-Forwarded-Proto "https"
EOF

for TYPE in std ssl; do
  DIR="/etc/apache2/conf.d/userdata/$TYPE/2_4/$USER/$DOMAIN"
  mkdir -p "$DIR"
  printf '%s\n' "$PROXY" > "$DIR/proxy.conf"
  echo "  wrote $DIR/proxy.conf"
done

# If SELinux is enforcing, Apache is blocked from connecting to the local app
# until this boolean is set. Harmless where SELinux is off.
if command -v setsebool >/dev/null 2>&1; then
  setsebool -P httpd_can_network_connect 1 2>/dev/null || true
fi

echo "== applying (ensure_vhost_includes + rebuild + graceful restart) =="
/usr/local/cpanel/scripts/ensure_vhost_includes --user="$USER" || true
/usr/local/cpanel/scripts/rebuildhttpdconf
/usr/local/cpanel/scripts/restartsrv_httpd

echo
echo "==================================================================="
echo "  Done. Open:  https://$DOMAIN"
echo "  If it shows the cPanel default page, give AutoSSL a minute, or"
echo "  check that $DOMAIN has a valid SSL cert in WHM."
echo "==================================================================="
