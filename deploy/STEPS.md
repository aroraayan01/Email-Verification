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

## Moving to a new server

This is the `xomexo.com` → `inboxx.work` move: a different box, a different
cPanel account, a different IP. The new domain already resolves to the new
server, so there is no DNS change — the work is standing the app up there and
carrying across what git does not track.

**Do this first, in parallel with everything else: get a PTR record.**
Ask the host to set reverse DNS for the new IP to a hostname you control (e.g.
`srv.inboxx.work`), and add a matching `A` record for it. Tier 3 refuses to
run honestly without it — probing from an IP with no forward-confirmed rDNS
earns rejections that look exactly like dead mailboxes, which is the one error
this engine must never make. `install_app.sh` therefore ships `ENABLE_SMTP=0`;
turn it on only once this resolves:

```bash
dig +short -x <new-ip>          # must return your hostname
dig +short <that-hostname>      # must return <new-ip>
```

Everything below works regardless; only tier 3 waits.

### On the new server (WHM Terminal)

**1. Install the app.**

```bash
mkdir -p /opt/email-verifier && cd /opt/email-verifier
```

```bash
git clone https://github.com/aroraayan01/Email-Verification.git .
```

```bash
bash deploy/install_app.sh
```

It fetches an isolated Python 3.11, installs dependencies, writes a fresh
`app.env` (SMTP off, cache on, domain set to `inboxx.work`) and starts the
service on `127.0.0.1:8000`. Copy the password it prints.

**2. Carry the data across.** Code comes from git; this moves what git cannot
— accounts, API keys, activity, and the verdict cache *including every Clearout
verdict already paid for*:

```bash
bash deploy/migrate_from.sh root@134.195.138.179
```

It backs up whatever is already here, stops the service for the copy, restarts
it, reports what came over, and prints the two secrets worth carrying from the
old `app.env` (`APP_SECRET`, `CLEAROUT_API_KEY`).

**3. Paste those two into `app.env`**, plus check `PORT` and the domain lines:

```bash
nano /opt/email-verifier/app.env
```

```bash
systemctl restart email-verifier
```

**4. Point the domain at it** — use this server's cPanel username, not
`grapme`:

```bash
USER_CPANEL=<new-cpanel-user> bash /opt/email-verifier/deploy/setup_proxy.sh inboxx.work
```

Then let AutoSSL issue a certificate for `inboxx.work` in WHM.

**5. Rename the admin account** so `ADMIN_EMAIL` matches the row that came
over, rather than seeding a second admin beside it:

```bash
/opt/email-verifier/.venv/bin/python -c "import sqlite3;c=sqlite3.connect('/opt/email-verifier/webdata/users.sqlite3');c.execute(\"UPDATE users SET email='admin@inboxx.work' WHERE email='admin@xomexo.com'\");c.commit();print('renamed')"
```

**6. Mail authentication.** The new domain needs its own SPF and DKIM records
on this server (cPanel → Email Deliverability → Repair). Skip this and every
verification code and password-reset mail lands in spam — the app will look
broken to anyone signing up, and nothing in the logs will say why.

### Verify before cutting over

On the new server, in this order:

1. `https://inboxx.work` loads the app (not a placeholder, no cert warning).
2. Sign in as `admin@inboxx.work` with the old password.
3. `/admin` shows your users **and** the Clearout credit balance — that one
   number proves the key, the cache and the vendor tier all survived.
4. Send yourself a password reset and confirm it arrives, not in spam.
5. Verify one address end to end.

### Only then, retire the old server

On the **old** server:

```bash
bash /opt/email-verifier/deploy/retire_domain.sh xomexo.com
```

That removes only the proxy include; DNS and the certificate are untouched, and
`setup_proxy.sh xomexo.com` puts it straight back. Leave the old service
running and the data in place for a week or so before decommissioning — it is
the only copy of anything the migration silently missed.

Once tier 3's PTR record resolves on the new host, set `ENABLE_SMTP=1` in
`app.env` and restart. Confirm with a handful of known-good and known-bad
addresses before trusting a full list from the new IP.
