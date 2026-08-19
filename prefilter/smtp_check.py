"""SMTP mailbox probing.

Opens a conversation with the recipient's MX, gets as far as RCPT TO, and
quits before DATA -- so nothing is ever delivered.

The classification rule is the whole point of this module: a rejection only
means "this mailbox does not exist" if it arrives *in response to RCPT TO* on
a connection that was otherwise accepted. A rejection at connect, banner, EHLO
or MAIL FROM is a verdict on the prober's own IP, and must never be recorded
against the address.
"""

import asyncio
import random
import re
import string
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import dns.asyncresolver
import dns.exception
import dns.resolver

# Outcomes
VALID = "valid"
INVALID = "invalid"
CATCH_ALL = "catch_all"
UNKNOWN = "unknown"
BLOCKED = "blocked"      # the far end refused *us*, not the address

# Ported from the server-validated prober (15/23 at 100% precision). The naive
# "any 5xx at RCPT means no such mailbox" rule misreads policy rejections like
# "5.7.1 blocked" as dead mailboxes; these two guards prevent that.
_ENHANCED = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")

# "We are refusing YOU", not "that mailbox is absent". Deliberately excludes
# bare "denied" and 5.4.x -- Microsoft says "5.4.1 Access denied" for addresses
# that genuinely do not exist.
_POLICY_RE = re.compile(
    r"5\.7\.\d"
    r"|blocked|block list|blacklist|black listed|banned"
    r"|spamhaus|spamcop|proofpoint|barracuda|mimecast|senderscore"
    r"|reputation|bad sender|poor sender|sender is not allowed"
    r"|not allowed to send|relay(?:ing)? denied|relay access denied"
    r"|rate limit|too many|try again later|temporarily deferred"
    # Sender-callout verification: the server is rejecting OUR sender, not the
    # recipient. Never a verdict about the address being probed.
    r"|sender verif|verify failed|verification failed|callout|greylist", re.I)


def _signature(detail: str) -> Tuple[str, str]:
    """Reduce a reply to its meaning, dropping per-message/per-server noise.

    Two rejections with the same signature are the server saying the same thing
    about two different addresses -- which is how we tell 'no such mailbox'
    (matches the known-fake) from anything else.
    """
    enhanced = _ENHANCED.search(detail)
    text = detail.lower()
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+", " ", text)
    text = re.sub(r"\b[\w-]+(?:\.[\w-]+){2,}\b", " ", text)
    text = re.sub(r"\S*\d\S*", " ", text)
    text = re.sub(r"[^a-z ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (enhanced.group(1) if enhanced else "", text)


@dataclass
class ProbeResult:
    email: str
    status: str
    stage: str = ""          # where the conversation ended
    code: int = 0
    detail: str = ""
    mx: str = ""
    catch_all: Optional[bool] = None


@dataclass
class ProbeConfig:
    helo: str = "localhost"
    # Empty envelope sender (<>). Servers skip sender-callout verification on
    # the null sender -- using a real address that fails that callout gets the
    # whole probe rejected as "Sender verify failed", masking valid mailboxes.
    mail_from: str = ""
    timeout: float = 20.0
    per_domain_delay: float = 1.5     # politeness gap between probes to one MX
    domain_concurrency: int = 8       # distinct domains probed at once
    retries: int = 1


def _random_local(length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


async def _read_response(reader: asyncio.StreamReader,
                         timeout: float) -> Tuple[int, str]:
    """Read one (possibly multiline) SMTP reply."""
    lines: List[str] = []
    while True:
        raw = await asyncio.wait_for(reader.readline(), timeout)
        if not raw:
            raise ConnectionError("connection closed by peer")
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(text)
        # Continuation lines are "250-text"; the final line is "250 text".
        if len(text) < 4 or text[3] != "-":
            break
    last = lines[-1]
    code = int(last[:3]) if last[:3].isdigit() else 0
    return code, " | ".join(lines)[:300]


async def _send(writer: asyncio.StreamWriter, line: str) -> None:
    writer.write((line + "\r\n").encode("utf-8"))
    await writer.drain()


async def _probe_domain(domain: str, addresses: Sequence[str],
                        config: ProbeConfig,
                        resolver: "dns.asyncresolver.Resolver") -> List[ProbeResult]:
    """Probe every address on one domain over a single connection."""
    # MX in preference order -- lowest number first.
    try:
        answer = await resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in
                 sorted(answer, key=lambda r: r.preference)]
        hosts = [h for h in hosts if h and h != "."]
    except dns.resolver.NXDOMAIN:
        return [ProbeResult(a, INVALID, "dns", 0, "NXDOMAIN") for a in addresses]
    except Exception as exc:  # noqa: BLE001
        return [ProbeResult(a, UNKNOWN, "dns", 0, str(exc)[:80]) for a in addresses]

    if not hosts:
        # RFC 7505 null MX, or MX-less domain -- fall back to the A record.
        hosts = [domain]

    mx = hosts[0]
    results: List[ProbeResult] = []

    def all_as(status: str, stage: str, code: int, detail: str) -> List[ProbeResult]:
        return [ProbeResult(a, status, stage, code, detail, mx) for a in addresses]

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(mx, 25), config.timeout)

        code, detail = await _read_response(reader, config.timeout)
        if code != 220:
            # Refused at the door. This is about our IP, not their mailboxes.
            return all_as(BLOCKED, "banner", code, detail)

        await _send(writer, "EHLO " + config.helo)
        code, detail = await _read_response(reader, config.timeout)
        if code != 250:
            await _send(writer, "HELO " + config.helo)
            code, detail = await _read_response(reader, config.timeout)
            if code != 250:
                return all_as(BLOCKED, "ehlo", code, detail)

        await _send(writer, "MAIL FROM:<{0}>".format(config.mail_from))
        code, detail = await _read_response(reader, config.timeout)
        if code != 250:
            return all_as(BLOCKED, "mail_from", code, detail)

        # Catch-all control: a random address that cannot plausibly exist. Its
        # rejection becomes this domain's fingerprint for "no such mailbox".
        control = "{0}@{1}".format(_random_local(), domain)
        await _send(writer, "RCPT TO:<{0}>".format(control))
        code, detail = await _read_response(reader, config.timeout)
        if code in (250, 251):
            is_catch_all = True
            baseline = None
        elif 500 <= code < 600 and _POLICY_RE.search(detail):
            # The fake was refused for a reason about US (sender verify, block).
            # Nothing on this connection tells us anything about a mailbox.
            return all_as(BLOCKED, "control", code, detail)
        elif 500 <= code < 600:
            is_catch_all = False
            baseline = _signature(detail)
        else:
            # 4xx on the control -- greylisting or rate limiting. Nothing on
            # this connection can be trusted.
            return all_as(UNKNOWN, "control", code, detail)

        # Google throttles a fast prober -- it starts deferring with 4xx, which
        # would waste the whole run as "unknown". Probing its MX needs a wider
        # gap between RCPTs than an ordinary server does.
        is_google = "google" in mx.lower()
        rcpt_delay = max(config.per_domain_delay, 3.0) if is_google \
            else config.per_domain_delay

        for address in addresses:
            if is_catch_all:
                results.append(ProbeResult(address, CATCH_ALL, "control", code,
                                           "accepts any local part", mx, True))
                continue
            await asyncio.sleep(rcpt_delay)
            await _send(writer, "RCPT TO:<{0}>".format(address))
            code, detail = await _read_response(reader, config.timeout)
            if code in (250, 251):
                status = VALID
            elif 400 <= code < 500:
                status = UNKNOWN       # greylisted -- retry later, not a verdict
            elif 500 <= code < 600:
                if _POLICY_RE.search(detail):
                    status = BLOCKED   # refusing us, says nothing about the box
                elif _signature(detail) == baseline:
                    status = INVALID   # same rejection as the known-fake
                else:
                    status = UNKNOWN   # a different 5xx -- don't guess
            else:
                status = UNKNOWN
            results.append(ProbeResult(address, status, "rcpt", code, detail,
                                       mx, False))

        try:
            await _send(writer, "QUIT")
        except Exception:  # noqa: BLE001
            pass
        return results

    except asyncio.TimeoutError:
        return all_as(UNKNOWN, "timeout", 0, "timed out")
    except (ConnectionError, OSError) as exc:
        return all_as(BLOCKED, "connect", 0, "{0}: {1}".format(
            type(exc).__name__, str(exc)[:60]))
    except Exception as exc:  # noqa: BLE001
        return all_as(UNKNOWN, "error", 0, str(exc)[:80])
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


async def probe_all(emails: Sequence[str], config: ProbeConfig,
                    progress=None) -> List[ProbeResult]:
    by_domain: Dict[str, List[str]] = defaultdict(list)
    for email in emails:
        if "@" in email:
            by_domain[email.rsplit("@", 1)[1].lower()].append(email)

    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = 10
    resolver.lifetime = 10
    semaphore = asyncio.Semaphore(config.domain_concurrency)
    done = [0]
    total = len(by_domain)

    async def worker(domain: str, addresses: List[str]) -> List[ProbeResult]:
        async with semaphore:
            out = await _probe_domain(domain, addresses, config, resolver)
            done[0] += 1
            if progress:
                progress(done[0], total, domain)
            return out

    chunks = await asyncio.gather(
        *(worker(d, a) for d, a in by_domain.items()))
    return [result for chunk in chunks for result in chunk]
