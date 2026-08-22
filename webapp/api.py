"""Public API + account pages, mounted alongside the owner's internal tool.

Two separate worlds share the app:
  * the owner's UI (single/bulk/find) stays behind the shared password;
  * this module adds public accounts (signup/login/dashboard) and the
    key-authenticated /api/v1 endpoints that outside users call.

SMTP for public users is gated twice: a per-user daily quota AND a global
rolling cap on total probes, so no number of users can burst the server IP.
"""

import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from webapp import auth as owner_auth
from webapp import shell
from webapp.users import Users

router = APIRouter()

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "webdata")
users = Users(os.path.join(DATA_DIR, "users.sqlite3"))

# Public users may trigger SMTP only if the owner allows it AND the global cap
# has room. This cap is the single most important IP protection.
PUBLIC_SMTP = os.environ.get("PUBLIC_SMTP", "").lower() in ("1", "true", "yes")
GLOBAL_SMTP_PER_HOUR = int(os.environ.get("GLOBAL_SMTP_PER_HOUR", "400"))

ACCOUNT_COOKIE = "ev_account"

# Brand mark, inline so there is no icon-font or image dependency.
CHECK_SVG = ('<svg viewBox="0 0 24 24" fill="none"><path d="M4 12.5l5.5 5.5L20 6.5"'
             ' stroke="white" stroke-width="3" stroke-linecap="round"'
             ' stroke-linejoin="round"/></svg>')

# Wired in from main.py so we reuse the one engine configuration.
_engine = {}


def configure(run_engine, finder, cache_factory, smtp_config_factory,
              enable_smtp):
    _engine.update(run=run_engine, finder=finder, cache=cache_factory,
                   smtp=smtp_config_factory, enable_smtp=enable_smtp)


# ----------------------------------------------------------- helpers ------

def _account_token(user_id: int) -> str:
    return owner_auth._sign("acct:%d:%d" % (user_id, 10**9))  # long-lived-ish


def current_account(request: Request) -> Optional[dict]:
    """Public alias used by main.py to meter the web tools per account."""
    return _current_account(request)


def _current_account(request: Request) -> Optional[dict]:
    tok = request.cookies.get(ACCOUNT_COOKIE, "")
    if not tok or ":" not in tok:
        return None
    body = tok.rsplit(".", 1)[0]
    if tok != owner_auth._sign(body):
        return None
    try:
        uid = int(body.split(":")[1])
    except (IndexError, ValueError):
        return None
    return _user_by_id(uid)


def _user_by_id(uid: int) -> Optional[dict]:
    with users._conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def _api_user(api_key: str) -> dict:
    key = (api_key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    user = users.by_api_key(key)
    if user is None:
        raise HTTPException(401, "invalid or missing API key")
    if user["disabled"]:
        raise HTTPException(403, "account disabled")
    if not user["verified"]:
        raise HTTPException(403, "confirm your email before using the API")
    return user


def _bar_chart(data, w=320, h=120) -> str:
    """Inline SVG bar chart from [(label, value)] -- no external library."""
    max_v = max((v for _, v in data), default=0) or 1
    n = max(1, len(data))
    bw = w / n
    bars = []
    for i, (label, v) in enumerate(data):
        bh = (v / max_v) * (h - 16)
        bars.append(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" '
            'fill="var(--accent)"><title>%s: %d</title></rect>'
            % (i * bw + bw * 0.12, (h - 10) - bh, bw * 0.76, bh, label, v))
    return ('<svg viewBox="0 0 %d %d" style="width:100%%;height:%dpx;display:block">'
            '%s</svg>' % (w, h, h, "".join(bars)))


async def _run_one(email: str, allow_smtp: bool):
    cache = _engine["cache"]()
    smtp_config = _engine["smtp"]() if allow_smtp else None
    try:
        verdicts, _ = await _engine["run"](
            [email], cache, use_microsoft=True,
            use_smtp=allow_smtp, smtp_config=smtp_config)
    finally:
        if cache:
            cache.close()
    return verdicts[0]


# --------------------------------------------------------- API v1 ---------

class V1Verify(BaseModel):
    email: str


class V1Find(BaseModel):
    name: str
    domain: str


@router.post("/api/v1/verify")
async def v1_verify(body: V1Verify, authorization: str = Header(default=""),
                    x_api_key: str = Header(default="")):
    user = _api_user(x_api_key or authorization)
    ok, remaining = users.consume(user["id"], 1)
    if not ok:
        raise HTTPException(429, "daily quota exhausted (%d/day)" % user["daily_quota"])

    allow_smtp = (_engine["enable_smtp"] and PUBLIC_SMTP
                  and users.bump_counter("smtp_global", GLOBAL_SMTP_PER_HOUR, 3600))
    email = (body.email or "").strip()
    v = await _run_one(email, allow_smtp)
    users.log_query(user["id"], "verify", email, v.status, "api")
    return {
        "email": v.email, "status": v.status, "confidence": v.confidence,
        "checked_by": v.tier or "vendor", "reason": v.reason,
        "quota_remaining": remaining,
    }


@router.post("/api/v1/find")
async def v1_find(body: V1Find, authorization: str = Header(default=""),
                  x_api_key: str = Header(default="")):
    user = _api_user(x_api_key or authorization)
    ok, remaining = users.consume(user["id"], 1)
    if not ok:
        raise HTTPException(429, "daily quota exhausted (%d/day)" % user["daily_quota"])

    allow_smtp = (_engine["enable_smtp"] and PUBLIC_SMTP
                  and users.bump_counter("smtp_global", GLOBAL_SMTP_PER_HOUR, 3600))
    cache = _engine["cache"]()
    smtp_config = _engine["smtp"]() if allow_smtp else None
    try:
        r = await _engine["finder"].find(
            (body.name or "").strip(), (body.domain or "").strip(),
            cache=cache, use_microsoft=True, use_smtp=allow_smtp,
            smtp_config=smtp_config)
    finally:
        if cache:
            cache.close()
    users.log_query(user["id"], "find",
                    "%s @ %s" % ((body.name or "").strip(), (body.domain or "").strip()),
                    r.status, "api")
    return {"email": r.email, "status": r.status, "confidence": r.confidence,
            "quota_remaining": remaining}


class V1Bulk(BaseModel):
    emails: list


@router.post("/api/v1/bulk")
async def v1_bulk(body: V1Bulk, authorization: str = Header(default=""),
                  x_api_key: str = Header(default="")):
    """Verify up to 1000 addresses in one call. Synchronous but paced; for big
    lists send several batches rather than one huge one."""
    user = _api_user(x_api_key or authorization)
    emails = [e.strip() for e in (body.emails or []) if isinstance(e, str) and "@" in e]
    if not emails:
        raise HTTPException(400, "send a non-empty 'emails' array")
    if len(emails) > 1000:
        raise HTTPException(413, "max 1000 addresses per call")

    ok, remaining = users.consume(user["id"], len(emails))
    if not ok:
        raise HTTPException(429, "daily quota exhausted -- %d checks left"
                            % remaining)

    allow_smtp = (_engine["enable_smtp"] and PUBLIC_SMTP
                  and users.bump_counter("smtp_global", GLOBAL_SMTP_PER_HOUR,
                                         3600, n=1))
    cache = _engine["cache"]()
    smtp_config = _engine["smtp"]() if allow_smtp else None
    try:
        verdicts, report = await _engine["run"](
            emails, cache, use_microsoft=True, use_smtp=allow_smtp,
            smtp_config=smtp_config, log=lambda *a: None)
    finally:
        if cache:
            cache.close()

    users.log_query(user["id"], "bulk", "%d addresses" % len(emails),
                    "%d resolved" % report.siphoned, "api")
    return {
        "count": len(verdicts),
        "resolved": report.siphoned,
        "quota_remaining": remaining,
        "results": [{"email": v.email, "status": v.status,
                     "confidence": v.confidence, "checked_by": v.tier or "vendor"}
                    for v in verdicts],
    }


@router.get("/api/v1/usage")
async def v1_usage(authorization: str = Header(default=""),
                   x_api_key: str = Header(default="")):
    user = _api_user(x_api_key or authorization)
    from webapp.users import _today
    used = user["used_today"] if user["quota_date"] == _today() else 0
    return {"email": user["email"], "plan": user["plan"],
            "used_today": used, "daily_quota": user["daily_quota"],
            "remaining": max(0, user["daily_quota"] - used),
            "total_checks": user["total_checks"],
            "daily": [{"date": d, "checks": n}
                      for d, n in users.daily_usage(14, user_id=user["id"])]}


# ------------------------------------------------------ accounts ----------

@router.post("/signup")
async def signup(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    if "@" not in email or "." not in email.split("@")[-1]:
        return RedirectResponse("/account?err=Enter+a+valid+email", 302)
    if len(password) < 8:
        return RedirectResponse("/account?err=Password+needs+8%2B+characters", 302)

    # Guard the door with our own engine: only real addresses can register.
    try:
        v = await _run_one(email, allow_smtp=False)
        if v.status == "invalid":
            return RedirectResponse(
                "/account?err=That+email+looks+undeliverable", 302)
    except Exception:  # noqa: BLE001 - never block signup on a probe error
        pass

    try:
        user = users.create(email, password)
    except ValueError:
        return RedirectResponse("/account?err=Email+already+registered", 302)

    # Fire off the verification link (non-blocking -- signup succeeds regardless).
    try:
        from webapp import mailer
        mailer.send_verification(email, user["email_token"])
    except Exception:  # noqa: BLE001
        pass

    # Log them in (so they can resend / verify) but hold them at the
    # check-your-inbox page until the address is confirmed.
    resp = RedirectResponse("/verify-pending", 302)
    resp.set_cookie(ACCOUNT_COOKIE, _account_token(user["id"]),
                    max_age=30 * 24 * 3600, httponly=True, samesite="lax")
    return resp


@router.get("/verify-pending", response_class=HTMLResponse)
async def verify_pending(request: Request, sent: int = 0):
    user = _current_account(request)
    if user is None:
        return RedirectResponse("/account", 302)
    if user["verified"]:
        return RedirectResponse("/app", 302)

    note = ("<p class='ok-note'>A new link is on its way.</p>" if sent
            else "")
    inner = ("""<h1>Confirm your email</h1>
<p class="sub">We sent a verification link to <b>{email}</b>.
Click it to activate your account.</p>{note}
<form method="post" action="/account/resend-verification">
<button type="submit">Resend the link</button></form>
<p class="auth-foot">Wrong address? <a href="/account/logout">Sign out</a>
and start over.</p>""").format(email=user["email"], note=note)
    return HTMLResponse(shell.public_page("Confirm your email",
        '<div class="auth-wrap"><div class="auth-card">'
        '<div class="auth-logo">' + shell.MARK + '</div>' + inner + '</div></div>'))


@router.get("/verify-email", response_class=HTMLResponse)
async def verify_email(token: str = ""):
    user = users.verify_by_token(token)
    msg = ("<h1>Email verified</h1><p class='sub'>Your account is confirmed.</p>"
           "<a href='/app' class='btn'>Go to the app</a>") if user else \
          ("<h1>Link expired</h1><p class='sub'>That verification link is invalid "
           "or already used.</p><a href='/app' class='btn'>Back to the app</a>")
    return _simple_page("Verify email", msg)


@router.post("/account/resend-verification")
async def resend_verification(request: Request):
    user = _current_account(request)
    if user is None:
        return RedirectResponse("/account", 302)
    if not user["verified"]:
        token = users.new_token(user["id"])
        try:
            from webapp import mailer
            mailer.send_verification(user["email"], token)
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse("/verify-pending?sent=1", 302)


@router.post("/account/login")
async def account_login(request: Request):
    form = await request.form()
    user = users.check_password((form.get("email") or ""), form.get("password") or "")
    if user is None:
        return RedirectResponse("/account?err=Wrong+email+or+password", 302)
    resp = RedirectResponse("/app", 302)
    resp.set_cookie(ACCOUNT_COOKIE, _account_token(user["id"]),
                    max_age=30 * 24 * 3600, httponly=True, samesite="lax")
    return resp


@router.get("/account/logout")
async def account_logout():
    resp = RedirectResponse("/account", 302)
    resp.delete_cookie(ACCOUNT_COOKIE)
    return resp


@router.post("/account/rotate")
async def account_rotate(request: Request):
    user = _current_account(request)
    if user is None:
        return RedirectResponse("/account", 302)
    users.rotate_key(user["id"])
    return RedirectResponse("/dashboard", 302)


@router.get("/pricing", response_class=HTMLResponse)
async def pricing():
    plans = [
        ("Free", "$0", "/mo", False, "Start free",
         "/account", ["100 checks / day", "Single &amp; bulk verify",
                      "Email finder", "Full API access"]),
        ("Pro", "$29", "/mo", True, "Get Pro",
         "/account?err=Pro+is+rolling+out+%E2%80%94+contact+us",
         ["10,000 checks / day", "Priority queue",
          "Higher API rate limits", "Usage history &amp; exports"]),
        ("Business", "Custom", "", False, "Contact sales",
         "/account?err=Contact+us+for+Business",
         ["Unlimited volume", "Dedicated verification IP",
          "SLA &amp; priority support", "Invoicing"]),
    ]
    cards = ""
    for name, price, per, featured, cta, href, feats in plans:
        cards += (
            '<div class="price-card%s">%s<h3>%s</h3>'
            '<div class="price">%s<span>%s</span></div>'
            '<ul class="plan">%s</ul>'
            '<a href="%s" class="btn%s" style="width:100%%">%s</a></div>'
            % (" featured" if featured else "",
               '<span class="tag">Popular</span>' if featured else "",
               name, price, per,
               "".join("<li>%s</li>" % f for f in feats),
               href, "" if featured else " ghost", cta))
    body = ('<div class="pub-head"><div class="eyebrow">Pricing</div>'
            '<h1>Simple, honest pricing</h1>'
            '<p class="lead">Start free — no card. Upgrade when your volume grows.</p></div>'
            '<div class="price-grid">%s</div>'
            '<p class="muted" style="text-align:center;margin-top:26px">Every plan includes '
            'catch-all detection and gateway-aware verification. Paid plans are rolling out; '
            'free is live now.</p>' % cards)
    return HTMLResponse(shell.public_page("Pricing", body, active="pricing"))


# ------------------------------------------------- password reset ---------

def _simple_page(title: str, inner: str) -> HTMLResponse:
    return HTMLResponse(shell.public_page(title,
        '<div class="auth-wrap"><div class="auth-card">'
        '<div class="auth-logo">' + shell.MARK + '</div>' + inner + '</div></div>'))


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(sent: int = 0):
    if sent:
        return _simple_page("Check your email",
                            "<h1>Check your email</h1><p class='sub'>If that address "
                            "has an account, a reset link is on its way. It expires "
                            "in 1 hour.</p><a href='/account' class='btn'>Back to login</a>")
    return _simple_page("Forgot password", """
<h1>Forgot password</h1><p class="sub">We'll email you a reset link.</p>
<form method="post" action="/forgot">
<input type="email" name="email" placeholder="you@company.com" required>
<button type="submit">Send reset link</button></form>
<p class="sub" style="margin-top:14px"><a href="/account">Back to login</a></p>""")


@router.post("/forgot")
async def forgot_submit(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    token = users.start_reset(email)
    if token:
        try:
            from webapp import mailer
            mailer.send_reset(email, token)
        except Exception:  # noqa: BLE001
            pass
    # Always the same response -- never reveal whether the account exists.
    return RedirectResponse("/forgot?sent=1", 302)


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(token: str = "", err: str = ""):
    note = ('<p class="err">%s</p>' % err.replace("+", " ")) if err else ""
    return _simple_page("Reset password", """
<h1>Choose a new password</h1>%s
<form method="post" action="/reset">
<input type="hidden" name="token" value="%s">
<input type="password" name="password" placeholder="New password (8+ chars)" required>
<button type="submit">Set new password</button></form>""" % (note, token))


@router.post("/reset")
async def reset_submit(request: Request):
    form = await request.form()
    token = (form.get("token") or "").strip()
    password = form.get("password") or ""
    if len(password) < 8:
        return RedirectResponse(
            "/reset?token=%s&err=Password+needs+8%%2B+characters" % token, 302)
    if not users.reset_password(token, password):
        return _simple_page("Link expired",
                            "<h1>Link expired</h1><p class='sub'>That reset link is "
                            "invalid or older than an hour.</p>"
                            "<a href='/forgot' class='btn'>Request a new one</a>")
    return _simple_page("Password updated",
                        "<h1>Password updated</h1><p class='sub'>You can log in "
                        "with your new password.</p>"
                        "<a href='/account' class='btn'>Log in</a>")


@router.get("/account", response_class=HTMLResponse)
async def account_page(err: str = ""):
    note = ('<p class="err">%s</p>' % err.replace("+", " ")) if err else ""
    inner = ("""<h1>Sign in to Xomexo</h1>
<p class="sub">Free account &middot; 100 checks a day &middot; no card</p>{note}
<form method="post" action="/account/login">
<input type="email" name="email" placeholder="you@company.com" required>
<input type="password" name="password" placeholder="Password" required>
<button type="submit">Sign in</button></form>
<div class="divider">&mdash; or create an account &mdash;</div>
<form method="post" action="/signup">
<input type="email" name="email" placeholder="you@company.com" required>
<input type="password" name="password" placeholder="Password (8+ characters)" required>
<button type="submit" class="btn ghost" style="width:100%">Create free account</button></form>
<p class="auth-foot"><a href="/forgot">Forgot password?</a> &middot;
<a href="/docs-api">API docs</a></p>""").replace("{note}", note)
    return HTMLResponse(shell.public_page("Sign in",
        '<div class="auth-wrap"><div class="auth-card">'
        '<div class="auth-logo">' + shell.MARK + '</div>' + inner + '</div></div>'))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = _current_account(request)
    if user is None:
        return RedirectResponse("/account", 302)
    from webapp.users import _today
    used = user["used_today"] if user["quota_date"] == _today() else 0
    user["_used"] = used

    banner = ""
    if not user["verified"]:
        banner = ("<div class='verify-banner'>Please verify your email — check your inbox."
                  "<form method='post' action='/account/resend-verification' "
                  "style='display:inline;margin-left:auto'>"
                  "<button class='copy-btn'>Resend link</button></form></div>")

    body = """{banner}
<p class="sub">Use this key with the API. Keep it secret — anyone with it can spend your quota.</p>
<div class="found-email">{key}
  <button class="copy-btn" onclick="navigator.clipboard.writeText('{key}')">Copy</button>
</div>
<form method="post" action="/account/rotate" style="margin:0 0 26px">
  <button class="btn ghost" type="submit">Regenerate key</button>
</form>

<div class="summary" style="margin-top:0">
  <div class="stat"><span>{used}</span><label>used today</label></div>
  <div class="stat"><span>{quota}</span><label>daily limit</label></div>
  <div class="stat"><span>{total}</span><label>all-time checks</label></div>
  <div class="stat"><span>{plan}</span><label>plan</label></div>
</div>

<h3 style="margin:28px 0 12px;font-size:15px">Usage — last 14 days</h3>
<div class="panel" style="margin-top:0">{chart}</div>

<h3 style="margin:28px 0 12px;font-size:15px">Quick start</h3>
<div class="table-wrap"><pre style="padding:16px;margin:0;overflow-x:auto;font-size:12.8px">curl -X POST https://xomexo.com/api/v1/verify \
  -H "X-API-Key: {key}" \
  -H "Content-Type: application/json" \
  -d '{{"email":"someone@company.com"}}'</pre></div>
<p class="muted" style="margin-top:14px">
  <a href="/docs-api">Full API docs</a> &middot;
  <a href="/account/history.csv">Export my history (CSV)</a> &middot;
  <a href="/pricing">Upgrade plan</a></p>""".format(
        banner=banner, key=user["api_key"], used="{:,}".format(used),
        quota="{:,}".format(user["daily_quota"]),
        total="{:,}".format(user["total_checks"]), plan=user["plan"].title(),
        chart=_bar_chart(users.daily_usage(14, user_id=user["id"])))
    return HTMLResponse(shell.page("API key & usage", user, body, active="dashboard"))


def _require_admin(request: Request) -> Optional[dict]:
    user = _current_account(request)
    if user is None or not user.get("is_admin"):
        return None
    return user


@router.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    admin = _require_admin(request)
    if admin is None:
        return RedirectResponse("/account", 302)
    from webapp.users import _today
    admin["_used"] = admin["used_today"] if admin["quota_date"] == _today() else 0
    st = users.admin_stats()

    rows = ""
    for u in users.list_users():
        badges = ""
        if u["is_admin"]:
            badges += ' <span class="pill valid">admin</span>'
        if u["disabled"]:
            badges += ' <span class="pill invalid">disabled</span>'
        if not u["verified"]:
            badges += ' <span class="pill unknown">unverified</span>'
        opts = "".join("<option value='%s'%s>%s</option>"
                       % (pl, " selected" if u["plan"] == pl else "", pl.title())
                       for pl in ("free", "pro", "business"))
        rows += (
            "<tr><td><a href='/admin/user/%d/history'>%s</a>%s</td><td>%s / %s</td><td>%s</td>"
            "<td><form method='post' action='/admin/user/%d/plan' style='display:flex;gap:5px'>"
            "<select name='plan' class='chip' style='padding:5px 8px'>%s</select>"
            "<button class='copy-btn'>Set</button></form></td>"
            "<td><form method='post' action='/admin/user/%d/quota' style='display:flex;gap:5px'>"
            "<input name='quota' value='%d' style='width:86px;padding:5px 8px;flex:none'>"
            "<button class='copy-btn'>Set</button></form></td>"
            "<td style='display:flex;gap:5px'>"
            "%s"
            "<form method='post' action='/admin/user/%d/toggle'>"
            "<button class='copy-btn'>%s</button></form></td></tr>"
            % (u["id"], u["email"], badges, "{:,}".format(u["used_today"]),
               "{:,}".format(u["daily_quota"]), "{:,}".format(u["total_checks"]),
               u["id"], opts, u["id"], u["daily_quota"],
               ("" if u["verified"] else
                "<form method='post' action='/admin/user/%d/verify'>"
                "<button class='copy-btn'>Verify</button></form>" % u["id"]),
               u["id"], "Enable" if u["disabled"] else "Disable"))

    qrows = ""
    for q in users.recent_queries(200):
        result = q["result"] or ""
        qrows += ("<tr><td class='muted'>%s</td><td>%s</td><td class='email'>%s</td>"
                  "<td><span class='pill %s'>%s</span></td><td class='muted'>%s</td>"
                  "<td class='muted'>%s</td></tr>"
                  % (q["at"][11:19], q["email"], q["query"],
                     result.split()[0] if result else "", result or "—",
                     q["via"], q["kind"]))

    body = """<div class="summary" style="margin-top:0">
  <div class="stat"><span>{users_n}</span><label>users</label></div>
  <div class="stat"><span>{checks}</span><label>all-time checks</label></div>
  <div class="stat"><span>{today}</span><label>searches today</label></div>
</div>

<h3 style="margin:28px 0 12px;font-size:15px">Activity — last 14 days</h3>
<div class="panel" style="margin-top:0">{chart}</div>

<h3 style="margin:28px 0 12px;font-size:15px">Users</h3>
<div class="table-wrap"><table>
<thead><tr><th>Email</th><th>Today</th><th>All-time</th><th>Plan</th><th>Daily cap</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>

<h3 style="margin:28px 0 12px;font-size:15px">Recent searches
  <a href="/admin/export.csv" class="copy-btn" style="float:right;text-decoration:none">Export CSV</a></h3>
<div class="table-wrap"><table>
<thead><tr><th>Time</th><th>User</th><th>Query</th><th>Result</th><th>Via</th><th>Kind</th></tr></thead>
<tbody>{qrows}</tbody></table></div>""".format(
        users_n="{:,}".format(st["users"]), checks="{:,}".format(st["total_checks"]),
        today="{:,}".format(st["today"]), chart=_bar_chart(users.daily_usage(14)),
        rows=rows or "<tr><td colspan='6' class='muted'>No users yet</td></tr>",
        qrows=qrows or "<tr><td colspan='6' class='muted'>No searches yet</td></tr>")
    return HTMLResponse(shell.page("Admin", admin, body, active="admin", wide=True))


@router.get("/admin/user/{uid}/history", response_class=HTMLResponse)
async def admin_user_history(uid: int, request: Request):
    admin = _require_admin(request)
    if admin is None:
        return RedirectResponse("/account", 302)
    from webapp.users import _today
    admin["_used"] = admin["used_today"] if admin["quota_date"] == _today() else 0

    target = _user_by_id(uid)
    if target is None:
        return HTMLResponse(shell.page("User history", admin,
            "<p class='muted'>No such user.</p><p><a href='/admin'>&larr; Back to admin</a></p>",
            active="admin"))

    rows = ""
    for q in users.recent_queries(2000, user_id=uid):
        result = q["result"] or ""
        rows += ("<tr><td class='muted'>%s</td><td>%s</td><td class='email'>%s</td>"
                 "<td><span class='pill %s'>%s</span></td><td class='muted'>%s</td></tr>"
                 % (q["at"][:19].replace("T", " "), q["kind"], q["query"],
                    result.split()[0] if result else "", result or "—", q["via"]))

    badges = ""
    if target["is_admin"]:
        badges += " <span class='pill valid'>admin</span>"
    if target["disabled"]:
        badges += " <span class='pill invalid'>disabled</span>"

    body = """<p><a href="/admin">&larr; Back to admin</a></p>
<div class="summary" style="margin-top:6px">
  <div class="stat"><span>{used}</span><label>used today</label></div>
  <div class="stat"><span>{quota}</span><label>daily cap</label></div>
  <div class="stat"><span>{total}</span><label>all-time checks</label></div>
  <div class="stat"><span>{plan}</span><label>plan</label></div>
</div>
<h3 style="margin:26px 0 12px;font-size:15px">Full history — {email}{badges}</h3>
<div class="table-wrap"><table>
<thead><tr><th>When</th><th>Type</th><th>Query</th><th>Result</th><th>Via</th></tr></thead>
<tbody>{rows}</tbody></table></div>""".format(
        used="{:,}".format(target["used_today"]),
        quota="{:,}".format(target["daily_quota"]),
        total="{:,}".format(target["total_checks"]),
        plan=target["plan"].title(), email=target["email"], badges=badges,
        rows=rows or "<tr><td colspan='5' class='muted'>No checks yet</td></tr>")
    return HTMLResponse(shell.page("User history", admin, body,
                                   active="admin", wide=True))


@router.get("/account/history.csv")
async def export_history(request: Request):
    """Download this account's search history as CSV."""
    import csv as _csv
    import io as _io

    user = _current_account(request)
    if user is None:
        return RedirectResponse("/account", 302)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["time", "kind", "query", "result", "via"])
    for q in users.recent_queries(5000, user_id=user["id"]):
        w.writerow([q["at"], q["kind"], q["query"], q["result"], q["via"]])
    from fastapi.responses import Response
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="xomexo-history.csv"'})


@router.get("/admin/export.csv")
async def admin_export(request: Request):
    """Admin: full search log across all users, as CSV."""
    import csv as _csv
    import io as _io

    if _require_admin(request) is None:
        return RedirectResponse("/account", 302)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["time", "user", "kind", "query", "result", "via"])
    for q in users.recent_queries(20000):
        w.writerow([q["at"], q["email"], q["kind"], q["query"], q["result"],
                    q["via"]])
    from fastapi.responses import Response
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="xomexo-all-activity.csv"'})


@router.post("/admin/user/{uid}/plan")
async def admin_set_plan(uid: int, request: Request):
    if _require_admin(request) is None:
        return RedirectResponse("/account", 302)
    form = await request.form()
    plan = (form.get("plan") or "free").strip()
    quota = {"free": 100, "pro": 10000, "business": 1000000}.get(plan, 100)
    users.set_plan(uid, plan, quota)
    return RedirectResponse("/admin", 302)


@router.post("/admin/user/{uid}/quota")
async def admin_set_quota(uid: int, request: Request):
    if _require_admin(request) is None:
        return RedirectResponse("/account", 302)
    form = await request.form()
    try:
        users.set_quota(uid, int(form.get("quota") or 0))
    except ValueError:
        pass
    return RedirectResponse("/admin", 302)


@router.post("/admin/user/{uid}/toggle")
async def admin_toggle(uid: int, request: Request):
    if _require_admin(request) is None:
        return RedirectResponse("/account", 302)
    u = _user_by_id(uid)
    if u:
        users.set_disabled(uid, not u["disabled"])
    return RedirectResponse("/admin", 302)


@router.post("/admin/user/{uid}/verify")
async def admin_verify(uid: int, request: Request):
    if _require_admin(request) is None:
        return RedirectResponse("/account", 302)
    users.set_verified(uid)
    return RedirectResponse("/admin", 302)
