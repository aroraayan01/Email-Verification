# Deploying to inboxx.work

Three pastes into WHM Terminal, in order. Each is safe: everything is isolated
to `/opt/email-verifier` and the `inboxx.work` vhost. Nothing touches your other
sites or the system Python.

## Step 1 — deliver the code
Paste the entire contents of **`paste_1_code.txt`**.
It writes and unpacks the app. Check the `md5sum` line matches the
`expected md5` printed just above it. If they differ, the paste truncated — clear
and paste again.

## Step 2 — install and start the service
```bash
cd /opt/email-verifier && bash deploy/install_app.sh
```
This fetches an isolated Python 3.11 (via `uv`), installs dependencies, and
starts the service on `127.0.0.1:8000`. It prints your **login password** at the
end — copy it. (~2–4 minutes.)

## Step 3 — point inboxx.work at it
```bash
bash /opt/email-verifier/deploy/setup_proxy.sh
```
Wires Apache so `https://inboxx.work` serves the app.

## Done
Open **https://inboxx.work**, log in with the printed password.

The SMTP tier is ON here (the server IP is clean), so private-domain and Gmail
addresses get real checks instead of guesses.

---

### Handy commands
```bash
systemctl status email-verifier          # is it running?
journalctl -u email-verifier -n 50        # recent logs
nano /opt/email-verifier/app.env          # change password, then:
systemctl restart email-verifier
```

### To update the app later
Re-run Step 1 with a fresh `paste_1_code.txt`, then:
```bash
systemctl restart email-verifier
```
Your cache and password survive (they live in `webdata/` and `app.env`).

---

## Updating after the first install (the easy way)

Once installed, every future update is **one command** on the server:

```bash
bash /opt/email-verifier/deploy/deploy.sh
```

It pulls the latest code from GitHub, refreshes dependencies, and restarts the
service. Your `app.env` (password/secret) and `webdata/` (accounts, history,
cache) are untracked by git, so they are never touched.

The normal loop is now:

1. **Locally:** make changes, commit, `git push`.
2. **On the server:** `bash /opt/email-verifier/deploy/deploy.sh`.

The first time you run it, it converts the existing `/opt/email-verifier`
tarball install into a git checkout in place (data preserved) — no manual steps.
The base64 paste flow (`paste_update.txt`) still works as a fallback if the
server ever can't reach GitHub.

---

## Moving to a new domain

Done once, when the product moves address (this is how `xomexo.com` became
`inboxx.work`). Order matters: DNS and the certificate have to be in place
before the app starts sending links that point at the new name.

**1. Registrar** — point the domain at the server:

```
A    inboxx.work       134.195.138.179
A    www.inboxx.work   134.195.138.179
```

**2. cPanel** — add the domain to the `grapme` account (Addon or Parked), then
let AutoSSL issue a certificate for it. Wait until `https://` on the new domain
loads *anything* without a certificate warning before continuing.

**3. Rename the admin account** *before* restarting, so the app finds the
existing row instead of seeding a second admin next to it:

```bash
/opt/email-verifier/.venv/bin/python -c "import sqlite3;c=sqlite3.connect('/opt/email-verifier/webdata/users.sqlite3');c.execute(\"UPDATE users SET email='admin@inboxx.work' WHERE email='admin@xomexo.com'\");c.commit();print('renamed')"
```

**4. `app.env`** — three lines, so links in signup and reset emails point at the
new name:

```
BASE_URL=https://inboxx.work
MAIL_FROM=no-reply@inboxx.work
ADMIN_EMAIL=admin@inboxx.work
```

**5. Mail authentication.** The new domain needs its own SPF and DKIM records
(cPanel → Email Deliverability → Repair). Skip this and every verification code
and password-reset mail lands in spam — the app will look broken to anyone
signing up, and nothing in the logs will say why.

**6. Wire the new domain, then retire the old one:**

```bash
bash /opt/email-verifier/deploy/setup_proxy.sh inboxx.work
```

```bash
bash /opt/email-verifier/deploy/deploy.sh
```

Check `https://inboxx.work` works — sign in, and send yourself a password reset
to confirm the mail path — *then*:

```bash
bash /opt/email-verifier/deploy/retire_domain.sh xomexo.com
```

`retire_domain.sh` only removes that domain's proxy include; its DNS and
certificate are untouched, and `setup_proxy.sh xomexo.com` puts it straight
back. Anything still pointing at the old host — bookmarks, saved API base
URLs — stops working at that moment, so retire it last.
