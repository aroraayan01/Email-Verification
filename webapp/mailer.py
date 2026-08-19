"""Send transactional email (verification links) via the local mail server.

Uses plain SMTP to localhost -- on the cPanel box, Exim accepts it and relays.
Deliverability to Gmail/Outlook depends on xomexo.com having SPF/DKIM set (cPanel
usually configures these for hosted domains). Sending failures never block
signup: the account is still created, the user just re-requests the link.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

MAIL_HOST = os.environ.get("MAIL_HOST", "localhost")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "25"))
MAIL_FROM = os.environ.get("MAIL_FROM", "no-reply@xomexo.com")
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
BASE_URL = os.environ.get("BASE_URL", "https://xomexo.com")


def send(to: str, subject: str, body_text: str, body_html: str = "") -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Xomexo <%s>" % MAIL_FROM
    msg["To"] = to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    try:
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=15) as s:
            try:
                s.starttls(context=ssl.create_default_context())
            except Exception:  # noqa: BLE001 - plain localhost relay is fine
                pass
            if MAIL_USER:
                s.login(MAIL_USER, MAIL_PASS)
            s.send_message(msg)
        return True
    except Exception:  # noqa: BLE001 - never let email break the signup flow
        return False


def send_reset(to: str, token: str) -> bool:
    link = "%s/reset?token=%s" % (BASE_URL, token)
    text = ("Reset your Xomexo password:\n%s\n\n"
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
    return send(to, "Reset your Xomexo password", text, html)


def send_verification(to: str, token: str) -> bool:
    link = "%s/verify-email?token=%s" % (BASE_URL, token)
    text = ("Welcome to Xomexo!\n\n"
            "Confirm your email to finish setting up your account:\n%s\n\n"
            "If you didn't sign up, ignore this message." % link)
    html = ("""<div style="font-family:sans-serif;max-width:480px;margin:auto">
<h2>Welcome to Xomexo ✓</h2>
<p>Confirm your email to finish setting up your account.</p>
<p><a href="%s" style="background:#2f6fed;color:#fff;padding:12px 22px;
border-radius:8px;text-decoration:none;display:inline-block">Verify my email</a></p>
<p style="color:#666;font-size:13px">Or paste this link: %s</p>
<p style="color:#999;font-size:12px">If you didn't sign up, ignore this.</p></div>"""
            % (link, link))
    return send(to, "Verify your Xomexo email", text, html)
