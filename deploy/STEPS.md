# Deploying to xomexo.com

Three pastes into WHM Terminal, in order. Each is safe: everything is isolated
to `/opt/email-verifier` and the `xomexo.com` vhost. Nothing touches your other
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

## Step 3 — point xomexo.com at it
```bash
bash /opt/email-verifier/deploy/setup_proxy.sh
```
Wires Apache so `https://xomexo.com` serves the app.

## Done
Open **https://xomexo.com**, log in with the printed password.

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
