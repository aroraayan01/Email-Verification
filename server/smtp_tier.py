#!/usr/bin/env python3
"""Tier 3: SMTP probing, production version. Runs on the server.

    python3 smtp_tier.py needs_smtp.csv smtp_results.csv

Python 3.6 compatible (no asyncio.run / capture_output / dataclasses).
Stdlib plus `dig`. Quits before DATA -- no mail is ever sent.

This is the calibrated v5 logic promoted to production. It only runs on
addresses the engine has already routed here: ordinary self-hosted mail
servers. Microsoft, Google and gateway domains are handled elsewhere or left
to the vendor, because SMTP cannot answer for them.

Verdict rules, in the order that matters:
  * A rejection at banner/EHLO/MAIL FROM is about THIS server's IP -> policy.
  * A fake local part that gets accepted -> catch-all, nothing is knowable.
  * Otherwise the fake's rejection becomes the domain's own "no such mailbox"
    fingerprint, and a real address is judged against it. Same fingerprint ->
    invalid. Anything else -> unknown, which escalates rather than rejects.
"""

import asyncio
import csv
import os
import random
import re
import string
import subprocess
import sys
from collections import Counter, defaultdict

TIMEOUT = 12.0
DOMAIN_CONCURRENCY = 10
PER_RCPT_DELAY = 0.8
MAX_MX_ATTEMPTS = 2
RETRY_DELAY = 300

HELO = os.environ.get("PROBE_HELO", "localhost")
MAIL_FROM = os.environ.get("PROBE_MAIL_FROM", "")  # empty sender skips sender-callout verification

VALID, INVALID, CATCH_ALL, UNKNOWN, POLICY = (
    "valid", "invalid", "catch_all", "unknown", "policy")

ENHANCED = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")

# Must NOT contain bare "denied" or 5.4.x -- Microsoft says "5.4.1 Access
# denied" for addresses that simply do not exist.
POLICY_RE = re.compile(
    r"5\.7\.\d"
    r"|blocked|block list|blacklist|black listed|banned"
    r"|spamhaus|spamcop|proofpoint|barracuda|mimecast|senderscore"
    r"|reputation|bad sender|poor sender|sender is not allowed"
    r"|not allowed to send|relay(?:ing)? denied|relay access denied"
    r"|rate limit|too many|try again later|temporarily deferred"
    r"|sender verif|verify failed|verification failed|callout", re.I)


def signature(detail):
    """Strip everything address- or connection-specific from a reply."""
    enhanced = ENHANCED.search(detail)
    text = detail.lower()
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+", " ", text)
    text = re.sub(r"\b\d+\.\d+\.\d+\.\d+\b", " ", text)
    text = re.sub(r"\b[\w-]+(?:\.[\w-]+){2,}\b", " ", text)
    text = re.sub(r"\S*\d\S*", " ", text)
    text = re.sub(r"[^a-z ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (enhanced.group(1) if enhanced else "", text)


def resolve_mx(domain):
    try:
        out = subprocess.run(["dig", "+short", "MX", domain],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=15).stdout
    except FileNotFoundError:
        sys.exit("`dig` not found -- install bind-utils/dnsutils.")
    except subprocess.TimeoutExpired:
        return None
    entries = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            entries.append((int(parts[0]), parts[1].rstrip(".")))
    if not entries:
        a = subprocess.run(["dig", "+short", "A", domain],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=15).stdout
        return [domain] if a.strip() else []
    return [h for _, h in sorted(entries) if h and h != "."]


async def read_reply(reader):
    lines = []
    while True:
        raw = await asyncio.wait_for(reader.readline(), TIMEOUT)
        if not raw:
            raise ConnectionError("closed by peer")
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(text)
        if len(text) < 4 or text[3] != "-":
            break
    last = lines[-1]
    return (int(last[:3]) if last[:3].isdigit() else 0), " | ".join(lines)[:200]


async def send(writer, line):
    writer.write((line + "\r\n").encode())
    await writer.drain()


async def rcpt(reader, writer, address):
    await asyncio.sleep(PER_RCPT_DELAY)
    await send(writer, "RCPT TO:<%s>" % address)
    return await read_reply(reader)


def fake_local():
    return ("".join(random.choice(string.ascii_lowercase) for _ in range(7))
            + "." + "".join(random.choice(string.ascii_lowercase) for _ in range(9))
            + str(random.randint(1000, 9999)))


async def attempt(mx, domain, addresses):
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(mx, 25), TIMEOUT)

        code, detail = await read_reply(reader)
        if code != 220:
            return [(a, POLICY, "banner", "%d %s" % (code, detail[:90]))
                    for a in addresses], True

        await send(writer, "EHLO " + HELO)
        code, detail = await read_reply(reader)
        if code != 250:
            await send(writer, "HELO " + HELO)
            code, detail = await read_reply(reader)
            if code != 250:
                return [(a, POLICY, "ehlo", "%d %s" % (code, detail[:90]))
                        for a in addresses], True

        await send(writer, "MAIL FROM:<%s>" % MAIL_FROM)
        code, detail = await read_reply(reader)
        if code != 250:
            return [(a, POLICY, "mail_from", "%d %s" % (code, detail[:90]))
                    for a in addresses], True

        neg_code, neg_detail = await rcpt(reader, writer,
                                          "%s@%s" % (fake_local(), domain))
        if 400 <= neg_code < 500:
            return [(a, UNKNOWN, "control", "%d %s" % (neg_code, neg_detail[:80]))
                    for a in addresses], False
        if neg_code in (250, 251):
            return [(a, CATCH_ALL, "control", "accepts a fake local part")
                    for a in addresses], False
        if POLICY_RE.search(neg_detail):
            # Fake refused for a reason about US (sender verify / block).
            return [(a, POLICY, "control", "%d %s" % (neg_code, neg_detail[:80]))
                    for a in addresses], True

        # Google throttles a fast prober into 4xx deferrals; widen the gap.
        is_google = "google" in mx.lower()
        baseline = signature(neg_detail)
        results = []
        for address in addresses:
            if is_google:
                await asyncio.sleep(2.5)
            code, detail = await rcpt(reader, writer, address)
            if code in (250, 251):
                status = VALID
            elif 400 <= code < 500:
                status = UNKNOWN
            elif 500 <= code < 600:
                if POLICY_RE.search(detail):
                    status = POLICY
                elif signature(detail) == baseline:
                    status = INVALID
                else:
                    status = UNKNOWN
            else:
                status = UNKNOWN
            results.append((address, status, "rcpt", "%d %s" % (code, detail[:90])))
        try:
            await send(writer, "QUIT")
        except Exception:
            pass
        return results, False

    except asyncio.TimeoutError:
        return [(a, UNKNOWN, "timeout", "timed out") for a in addresses], True
    except (ConnectionError, OSError) as exc:
        return [(a, POLICY, "connect", "%s: %s" % (type(exc).__name__, exc))
                for a in addresses], True
    except Exception as exc:
        return [(a, UNKNOWN, "error", str(exc)[:80]) for a in addresses], False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def probe_domain(domain, addresses):
    hosts = resolve_mx(domain)
    if hosts is None:
        return [(a, UNKNOWN, "dns", "dig timeout") for a in addresses]
    if not hosts:
        return [(a, INVALID, "dns", "no MX and no A record") for a in addresses]
    results = None
    for mx in hosts[:MAX_MX_ATTEMPTS]:
        results, retry = await attempt(mx, domain, addresses)
        if not retry:
            return results
    return results


async def main(in_path, out_path):
    emails = []
    with open(in_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        field = None
        for name in reader.fieldnames or []:
            if "email" in name.lower():
                field = name
                break
        if field is None:
            sys.exit("no email column found in " + in_path)
        for row in reader:
            value = (row[field] or "").strip()
            if "@" in value:
                emails.append(value.lower())

    by_domain = defaultdict(list)
    for email in emails:
        by_domain[email.rsplit("@", 1)[1]].append(email)

    print("probing %d addresses across %d domains" % (len(emails), len(by_domain)))
    print("HELO %s / MAIL FROM <%s>\n" % (HELO, MAIL_FROM))
    sys.stdout.flush()

    async def run_pass(groups, label):
        semaphore = asyncio.Semaphore(DOMAIN_CONCURRENCY)
        done = [0]
        total = len(groups)

        async def worker(domain, addresses):
            async with semaphore:
                out = await probe_domain(domain, addresses)
                done[0] += 1
                if done[0] % 10 == 0 or done[0] == total:
                    print("  %s %d/%d" % (label, done[0], total),
                          file=sys.stderr, flush=True)
                return out

        chunks = await asyncio.gather(*(worker(d, a) for d, a in groups.items()))
        return [r for chunk in chunks for r in chunk]

    results = await run_pass(by_domain, "pass1")

    retry_groups = defaultdict(list)
    for email, status, _stage, _detail in results:
        if status == UNKNOWN:
            retry_groups[email.rsplit("@", 1)[1]].append(email)
    if retry_groups:
        print("\n%d undecided; waiting %ds then retrying\n"
              % (sum(len(v) for v in retry_groups.values()), RETRY_DELAY))
        sys.stdout.flush()
        await asyncio.sleep(RETRY_DELAY)
        merged = dict((r[0], r) for r in results)
        for row in await run_pass(retry_groups, "retry"):
            if row[1] != UNKNOWN:
                merged[row[0]] = (row[0], row[1], row[2], row[3] + " [retry]")
        results = list(merged.values())

    with open(out_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["email", "status", "stage", "detail"])
        for row in results:
            writer.writerow(row)

    counts = Counter(r[1] for r in results)
    print("\n=== results ===")
    for status, n in counts.most_common():
        print("  %5d  %s" % (n, status))
    proven = counts.get(VALID, 0) + counts.get(INVALID, 0)
    print("\n  proven: %d of %d (%.0f%%)  -> these need no vendor credit"
          % (proven, len(results), 100.0 * proven / max(1, len(results))))
    print("  wrote %s" % out_path)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: python3 smtp_tier.py needs_smtp.csv smtp_results.csv")
    _loop = asyncio.get_event_loop()
    _loop.run_until_complete(main(sys.argv[1], sys.argv[2]))
    _loop.close()
