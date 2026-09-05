#!/bin/bash
# Stop serving the verifier on a domain -- the other half of setup_proxy.sh.
#
#     bash deploy/retire_domain.sh xomexo.com
#
# Removes ONLY that domain's reverse-proxy include, then rebuilds Apache. The
# domain, its DNS and its SSL certificate are left exactly as they are: it
# simply stops proxying to the app and falls back to whatever cPanel already
# serves for it. Reversible at any time with:
#
#     bash deploy/setup_proxy.sh xomexo.com
set -euo pipefail

USER="${USER_CPANEL:-grapme}"
DOMAIN="${1:-}"

if [ -z "$DOMAIN" ]; then
  echo "usage: bash deploy/retire_domain.sh <domain>" >&2
  exit 2
fi

echo "== retiring $DOMAIN (removing its proxy include only) =="

removed=0
for TYPE in std ssl; do
  DIR="/etc/apache2/conf.d/userdata/$TYPE/2_4/$USER/$DOMAIN"
  if [ -f "$DIR/proxy.conf" ]; then
    rm -f "$DIR/proxy.conf"
    rmdir "$DIR" 2>/dev/null || true      # only if now empty
    echo "  removed $DIR/proxy.conf"
    removed=$((removed + 1))
  else
    echo "  nothing at $DIR/proxy.conf (already gone)"
  fi
done

if [ "$removed" -eq 0 ]; then
  echo "== nothing to do; $DOMAIN was not proxied to this app =="
  exit 0
fi

echo "== applying (ensure_vhost_includes + rebuild + graceful restart) =="
/usr/local/cpanel/scripts/ensure_vhost_includes --user="$USER" || true
/usr/local/cpanel/scripts/rebuildhttpdconf
/usr/local/cpanel/scripts/restartsrv_httpd

echo
echo "==================================================================="
echo "  $DOMAIN no longer serves the verifier."
echo "  It now shows whatever cPanel has for that document root."
echo "  To put it back:  bash deploy/setup_proxy.sh $DOMAIN"
echo "==================================================================="
