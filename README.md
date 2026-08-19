# Xomexo — Email Verification & Finder

A self-hosted email verification service: verify addresses, find them from a
name and a domain, clean lists in bulk, and expose it all over an API — with a
web app, user accounts and an admin console.

It resolves roughly **half of a typical B2B list on its own**, and is honest
about the rest: catch-all and gateway-fronted domains are labelled as such
rather than guessed at.

---

## What it does

| | |
|---|---|
| **Verify** | One address, or a CSV/XLSX in bulk |
| **Find** | Name + domain → the person's address |
| **API** | Key-authenticated `verify` / `find` / `bulk` / `usage` |
| **Accounts** | Signup, login, password reset, email verification, per-user quotas |
| **Admin** | Users, plans, caps, enable/disable, activity log, CSV export |

## How verification works

Each address falls through progressively deeper checks and stops as soon as
something is **proven**:

1. **Local** — syntax, MX/DNS, disposable domains, dead domains, duplicates.
2. **Microsoft** — the `GetCredentialType` directory endpoint over HTTPS. No
   SMTP, so no IP reputation is involved. Covers roughly half of a B2B list.
   Also sees *through* Proofpoint/Mimecast gateways to the Microsoft tenant
   behind them, which pure-SMTP tools cannot.
3. **SMTP** — a live mailbox probe with a per-domain control test that exposes
   catch-all servers. Needs a clean sending IP (see below).
4. **Pattern** — for addresses nothing can settle, a clearly-labelled
   confidence score. Advisory only unless you opt in.

### The one design rule

> An address is only marked **invalid** on positive proof. Everything uncertain
> escalates.

A false negative costs one credit. A false positive is a hard bounce on a
customer's sending domain, so the asymmetry is deliberate.

---

## Running it

```bash
pip install -r requirements.txt
python -m uvicorn webapp.main:app --port 8000
```

Then open <http://127.0.0.1:8000>.

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | — | Seeds the admin account's password |
| `ADMIN_EMAIL` | `admin@xomexo.com` | Admin account address |
| `APP_SECRET` | random | Signs session cookies |
| `ENABLE_SMTP` | off | Enable the SMTP probing tier |
| `SMTP_HELO` | `localhost` | HELO name — must be a real FQDN with forward-confirmed rDNS |
| `SMTP_MAIL_FROM` | empty | Envelope sender. Empty (`<>`) skips sender-callout checks |
| `PUBLIC_SMTP` | off | Let API users trigger SMTP probing |
| `GLOBAL_SMTP_PER_HOUR` | `400` | Server-wide probe cap |
| `USE_CACHE` | on | Serve repeat lookups from the verdict cache |
| `BASE_URL` | `https://xomexo.com` | Used in verification/reset links |

### Deploying

`deploy/install_app.sh` sets up an isolated Python via `uv`, installs
dependencies, writes `app.env`, and runs the app as a systemd service.
`deploy/setup_proxy.sh` points a cPanel/Apache vhost at it.

---

## API

Authenticate with an `X-API-Key` header.

```bash
curl -X POST https://your-host/api/v1/verify \
  -H "X-API-Key: your_key" \
  -H "Content-Type: application/json" \
  -d '{"email":"someone@company.com"}'
```

```json
{"email":"someone@company.com","status":"valid",
 "checked_by":"microsoft","quota_remaining":99}
```

Endpoints: `POST /api/v1/verify`, `POST /api/v1/find`, `POST /api/v1/bulk`
(≤1000 per call), `GET /api/v1/usage`.

Statuses: `valid`, `invalid`, `catch_all`, `unknown`.
Errors: `401` bad key, `429` quota exhausted, `413` batch too large.

---

## About SMTP probing and IP reputation

The SMTP tier opens port 25 connections and quits before `DATA`, so **no mail
is ever sent**. It still needs care:

- Run it only from a host with a **clean, forward-confirmed** sending IP.
  From a residential connection it produced 14% agreement; from a properly
  configured server, 89%.
- Probing volume is what gets an IP blacklisted, not any single check. Keep
  batches modest and leave `GLOBAL_SMTP_PER_HOUR` in place.
- If you open signups to the public, move probing to a **dedicated,
  expendable IP** — never the host that sends your real mail.

## What it deliberately cannot do

It cannot tell you an address on a **catch-all** domain is dead. The receiving
server accepts everything, so the information does not exist to be read — by
anyone. Commercial services resolve those with accumulated cross-customer
bounce history, which is a data asset rather than an algorithm. Those addresses
are labelled `catch_all` and left for you to decide on.

---

## Layout

```
prefilter/    verification engine (routing, tiers, cache, finder)
webapp/       FastAPI app: UI, accounts, admin, public API
server/       standalone SMTP prober for a remote host (Python 3.6 compatible)
deploy/       install + reverse-proxy scripts
```

## Licence

No licence granted; all rights reserved.
