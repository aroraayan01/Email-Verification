#!/usr/bin/env python3
"""Standalone SMTP calibration probe (v4).

    python3 calibrate.py calibration_data.csv

Python 3.6 compatible. Stdlib plus `dig`. Quits before DATA -- no mail sent.

v4 replaces the global "does this 5xx mean no-such-user?" guesswork with a
per-domain baseline, because the same string means different things on
different servers. Microsoft answers "550 5.4.1 Access denied" for addresses
that do not exist; a blocked sender gets the same words. No regex can separate
those. Two control probes can:

    postmaster@   fake@      meaning
    ----------------------------------------------------------
    accept        reject     server discriminates -> trust it
    accept        accept     catch-all
    reject        reject     it is refusing US -> trust nothing

RFC 5321 s4.5.1 requires a mail-receiving domain to accept postmaster, so a
postmaster rejection is strong evidence we are the problem, not the address.

A real address is then judged against that baseline: same rejection signature
as the known-fake means the mailbox is genuinely absent; a *different*
rejection is not guessed at.
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
DOMAIN_CONCURRENCY = 12
PER_RCPT_DELAY = 0.8
MAX_MX_ATTEMPTS = 2
RETRY_DELAY = 300

HELO = os.environ.get("PROBE_HELO", "localhost")
MAIL_FROM = os.environ.get("PROBE_MAIL_FROM", "")

VALID, INVALID, CATCH_ALL, UNKNOWN, POLICY = (
    "valid", "invalid", "catch_all", "unknown", "policy")

ENHANCED = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")

# Language that means "we are refusing YOU" rather than "that mailbox is not
# here". Deliberately narrow: it must NOT contain bare "denied" or 5.4.x,
# because Microsoft says "5.4.1 Access denied" for addresses that don't exist.
POLICY_RE = re.compile(
    r"5\.7\.\d"
    r"|blocked|block list|blacklist|black listed|banned"
    r"|spamhaus|spamcop|proofpoint|barracuda|mimecast|senderscore"
    r"|reputation|bad sender|poor sender|sender is not allowed"
    r"|not allowed to send|relay(?:ing)? denied|relay access denied"
    r"|rate limit|too many|try again later|temporarily deferred", re.I)


def signature(detail):
    """Reduce a reply to its meaning, dropping anything address-specific.

    Two replies with the same signature are the server saying the same thing
    about two different addresses.
    """
    enhanced = ENHANCED.search(detail)
    text = detail.lower()
    # Bracketed diagnostics carry the receiving server's own hostname and a
    # timestamp -- unique per connection, so they must go before comparing.
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+", " ", text)
    text = re.sub(r"\b\d+\.\d+\.\d+\.\d+\b", " ", text)
    # Any remaining fully-qualified hostname (3+ labels).
    text = re.sub(r"\b[\w-]+(?:\.[\w-]+){2,}\b", " ", text)
    # Any token containing a digit is a per-message id, queue id or timestamp
    # (Google appends things like "g1-20240814"), never part of the meaning.
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
    return [host for _, host in sorted(entries) if host and host != "."]


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
    return "".join(random.choice(string.ascii_lowercase) for _ in range(7)) \
        + "." + "".join(random.choice(string.ascii_lowercase) for _ in range(9)) \
        + str(random.randint(1000, 9999))


async def attempt(mx, domain, addresses):
    """One conversation. Returns (results, retry_on_next_mx)."""
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

        # --- baseline: one address that must not exist, one that must ---
        neg_code, neg_detail = await rcpt(reader, writer,
                                          "%s@%s" % (fake_local(), domain))
        if 400 <= neg_code < 500:
            return [(a, UNKNOWN, "control", "%d %s" % (neg_code, neg_detail[:80]))
                    for a in addresses], False

        neg_accepted = neg_code in (250, 251)

        pos_code, pos_detail = await rcpt(reader, writer,
                                          "postmaster@%s" % domain)
        pos_accepted = pos_code in (250, 251)

        if neg_accepted:
            return [(a, CATCH_ALL, "control", "accepts a fake local part")
                    for a in addresses], False

        # postmaster is EVIDENCE, not a veto. RFC 5321 requires it, but hosted
        # tenants routinely never provision the mailbox, so rejecting it does
        # not mean the server is refusing us.
        pm = "ok" if pos_accepted else str(pos_code)

        # The fake was rejected, so this server distinguishes recipients.
        # That rejection is our baseline for "no such mailbox here".
        baseline = signature(neg_detail)
        results = []
        for address in addresses:
            code, detail = await rcpt(reader, writer, address)
            if code in (250, 251):
                status = VALID
            elif 400 <= code < 500:
                status = UNKNOWN
            elif 500 <= code < 600:
                same = signature(detail) == baseline
                if POLICY_RE.search(detail):
                    # Refusing us, whatever else it says.
                    status = POLICY
                elif same:
                    # Treated exactly like an address we know does not exist.
                    status = INVALID
                else:
                    status = UNKNOWN
            else:
                status = UNKNOWN
            results.append((address, status, "rcpt",
                            "%d %s | pm=%s" % (code, detail[:80], pm)))
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


async def main(path):
    truth = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            truth[row["email"].strip().lower()] = row["clearout_status"].strip().lower()

    by_domain = defaultdict(list)
    for email in truth:
        if "@" in email:
            by_domain[email.rsplit("@", 1)[1]].append(email)

    print("probing %d addresses across %d domains" % (len(truth), len(by_domain)))
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
                    print("  %s %d/%d domains" % (label, done[0], total),
                          file=sys.stderr, flush=True)
                return out

        chunks = await asyncio.gather(*(worker(d, a) for d, a in groups.items()))
        return [r for chunk in chunks for r in chunk]

    results = await run_pass(by_domain, "pass1")

    retry_groups = defaultdict(list)
    for email, status, _stage, _detail in results:
        if status == UNKNOWN and "@" in email:
            retry_groups[email.rsplit("@", 1)[1]].append(email)

    if retry_groups and RETRY_DELAY > 0:
        pending = sum(len(v) for v in retry_groups.values())
        print("\n%d undecided; waiting %ds then retrying them once\n"
              % (pending, RETRY_DELAY))
        sys.stdout.flush()
        await asyncio.sleep(RETRY_DELAY)
        merged = dict((r[0], r) for r in results)
        recovered = 0
        for row in await run_pass(retry_groups, "retry"):
            if row[1] != UNKNOWN:
                merged[row[0]] = (row[0], row[1], row[2], row[3] + " [retry]")
                recovered += 1
        results = list(merged.values())
        print("\n  retry resolved %d of %d previously undecided\n"
              % (recovered, pending))

    statuses, matrix = Counter(), Counter()
    for email, status, _stage, _detail in results:
        statuses[status] += 1
        matrix[(status, truth.get(email, "?"))] += 1

    print("=== our verdicts ===")
    for status, n in statuses.most_common():
        print("  %6d  %s" % (n, status))

    print("\n=== ours vs Clearout ===")
    print("  %-11s %-11s %s" % ("ours", "clearout", "n"))
    for (ours, vendor), n in sorted(matrix.items(), key=lambda kv: -kv[1]):
        flag = ""
        if ours == VALID and vendor == "invalid":
            flag = "   <-- WOULD BOUNCE"
        elif ours == INVALID and vendor == "valid":
            flag = "   <-- lost a real lead"
        print("  %-11s %-11s %d%s" % (ours, vendor, n, flag))

    said_invalid = sum(n for (o, _v), n in matrix.items() if o == INVALID)
    wrong_invalid = sum(n for (o, v), n in matrix.items()
                        if o == INVALID and v == "valid")
    said_valid = sum(n for (o, _v), n in matrix.items() if o == VALID)
    wrong_valid = sum(n for (o, v), n in matrix.items()
                      if o == VALID and v == "invalid")
    total_bad = sum(n for (_o, v), n in matrix.items() if v == "invalid")
    caught = sum(n for (o, v), n in matrix.items()
                 if o == INVALID and v == "invalid")

    print("\n=== the numbers that matter ===")
    if said_invalid:
        print("  called invalid : %d, of which %d were actually valid "
              "(precision %.1f%%)"
              % (said_invalid, wrong_invalid,
                 100.0 * (said_invalid - wrong_invalid) / said_invalid))
    else:
        print("  called invalid : 0  -- it never rejects anything, so it saves nothing")
    if said_valid:
        print("  called valid   : %d, of which %d would BOUNCE (%.1f%%)"
              % (said_valid, wrong_valid, 100.0 * wrong_valid / said_valid))
    print("  Clearout found %d bad addresses; we caught %d of them"
          % (total_bad, caught))
    decided = said_valid + said_invalid
    print("  decided %d of %d addresses (%.0f%% coverage)"
          % (decided, len(results), 100.0 * decided / max(1, len(results))))

    with open("calibration_results.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["email", "ours", "clearout", "stage", "detail"])
        for email, status, stage, detail in results:
            writer.writerow([email, status, truth.get(email, ""), stage, detail])
    print("\nwrote calibration_results.csv")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python3 calibrate.py calibration_data.csv")
    _loop = asyncio.get_event_loop()
    _loop.run_until_complete(main(sys.argv[1]))
    _loop.close()
