"""Send transactional email (verification links) via the local mail server.

Uses plain SMTP to localhost -- on the cPanel box, Exim accepts it and relays.
Deliverability to Gmail/Outlook depends on inboxx.work having SPF/DKIM set (cPanel
usually configures these for hosted domains). Sending failures never block
signup: the account is still created, the user just re-requests the link.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

MAIL_HOST = os.environ.get("MAIL_HOST", "localhost")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "25"))
MAIL_FROM = os.environ.get("MAIL_FROM", "no-reply@inboxx.work")
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
BASE_URL = os.environ.get("BASE_URL", "https://inboxx.work")


def send(to: str, subject: str, body_text: str, body_html: str = "") -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Inboxx <%s>" % MAIL_FROM
    msg["To"] = to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    # TLS only for an external submission relay (port 465/587 or authenticated).
    # A plain localhost relay (Exim on :25) must NOT use STARTTLS: its cert is
    # for the server hostname, not "localhost", so verification fails and leaves
    # the connection half-open -- which is why signup codes were silently lost.
    use_tls = bool(MAIL_USER) or MAIL_PORT in (465, 587)
    try:
        if MAIL_PORT == 465:
            smtp = smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=15,
                                    context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=15)
        with smtp as s:
            if use_tls and MAIL_PORT != 465:
                s.starttls(context=ssl.create_default_context())
            if MAIL_USER:
                s.login(MAIL_USER, MAIL_PASS)
            s.send_message(msg)
        return True
    except Exception:  # noqa: BLE001 - never let email break the signup flow
        return False


def send_reset(to: str, token: str) -> bool:
    link = "%s/reset?token=%s" % (BASE_URL, token)
    text = ("Reset your Inboxx password:\n%s\n\n"
            "This link expires in 1 hour. If you didn't ask for it, ignore "
            "this message." % link)
    html = ("""<div style="font-family:sans-serif;max-width:480px;margin:auto">
<h2>Reset your password</h2>
<p>Click below to choose a new password. The link expires in 1 hour.</p>
<p><a href="%s" style="background:#2f6fed;color:#fff;padding:12px 22px;
border-radius:8px;text-decoration:none;display:inline-block">Reset password</a></p>
<p style="color:#666;font-size:13px">Or paste this link: %s</p>
<p style="color:#999;font-size:12px">Didn't ask for this? Ignore it.</p></div>"""
            % (link, link))
    return send(to, "Reset your Inboxx password", text, html)


def send_verification(to: str, code: str) -> bool:
    """Email the signup confirmation code (OTP)."""
    text = ("Welcome to Inboxx!\n\n"
            "Your verification code is: %s\n\n"
            "Enter it on the confirmation page to activate your account. "
            "The code expires in 15 minutes.\n\n"
            "If you didn't sign up, ignore this message." % code)
    html = ("""<div style="font-family:sans-serif;max-width:460px;margin:auto;text-align:center">
<h2 style="margin-bottom:4px">Confirm your email</h2>
<p style="color:#555">Enter this code to finish setting up your Inboxx account.</p>
<div style="font-size:34px;font-weight:700;letter-spacing:10px;margin:22px 0;
padding:16px;background:#f2f5fb;border-radius:12px;color:#1f2937">%s</div>
<p style="color:#999;font-size:13px">The code expires in 15 minutes.
If you didn't sign up, ignore this email.</p></div>"""
            % code)
    return send(to, "Your Inboxx verification code: %s" % code, text, html)
