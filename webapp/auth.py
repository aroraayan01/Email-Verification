"""Minimal shared-password auth.

One password, set via the APP_PASSWORD env var. A correct login gets a signed
cookie (HMAC over an expiry timestamp, keyed by APP_SECRET) that the middleware
checks on every request. No accounts, no database -- enough to keep the verifier
off the open internet so strangers can't drive the server's SMTP probing.
"""

import hashlib
import hmac
import os
import time
from typing import Optional

COOKIE = "ev_session"
TTL = 7 * 24 * 3600            # a week


def _secret() -> bytes:
    # Falls back to a per-process random secret, which simply means everyone
    # is logged out on restart -- safe, just less convenient than setting one.
    return os.environ.get("APP_SECRET", "").encode() or os.urandom(32)


_SECRET = _secret()


def password() -> Optional[str]:
    return os.environ.get("APP_PASSWORD") or None


def _sign(expiry: str) -> str:
    mac = hmac.new(_SECRET, expiry.encode(), hashlib.sha256).hexdigest()
    return "{0}.{1}".format(expiry, mac)


def issue_token() -> str:
    return _sign(str(int(time.time()) + TTL))


def valid_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    expiry, _mac = token.split(".", 1)
    if token != _sign(expiry):
        return False
    try:
        return int(expiry) > int(time.time())
    except ValueError:
        return False


def check_password(candidate: str) -> bool:
    expected = password()
    if not expected:
        # No password configured -> auth disabled (e.g. local dev).
        return True
    return hmac.compare_digest(candidate or "", expected)


def auth_required() -> bool:
    return password() is not None
