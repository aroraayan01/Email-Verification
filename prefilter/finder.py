"""Email finder: name + domain -> the person's most likely address.

Given "John Smith" and "acme.com", generate the patterns companies actually
use, then verify them through the same tiers the verifier uses -- stopping at
the first real hit so a lookup is normally one or two probes, not a barrage.

The honest limit is the verifier's limit. On a domain we can probe (Microsoft,
non-catch-all SMTP) we FIND the real address. On a catch-all domain every
candidate "accepts", so we can't tell which is real -- we return the most
likely pattern as a labelled guess, which is where a data vendor (Hunter,
Clearout) would use accumulated history we don't have.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional

# Common corporate local-part formats, most-frequent first. {f}/{l} are first
# initials; {first}/{last} the full tokens. Order is the default priority when
# we have no learned format for the domain.
PATTERNS = [
    "{first}.{last}",
    "{f}{last}",
    "{first}{last}",
    "{first}",
    "{first}_{last}",
    "{f}.{last}",
    "{last}.{first}",
    "{first}{l}",
    "{last}{f}",
    "{f}{l}",
    "{last}",
    "{first}-{last}",
    "{last}{first}",
    "{f}-{last}",
    "{last}.{f}",
]


@dataclass
class Candidate:
    email: str
    pattern: str
    shape: str = ""       # structural shape, for format-priority matching


@dataclass
class FindResult:
    query: str
    domain: str
    email: Optional[str] = None
    status: str = "not_found"   # found | guess | not_found | unknown
    confidence: int = 0
    method: str = ""
    tried: int = 0
    candidates: List[str] = field(default_factory=list)


def _ascii(text: str) -> str:
    """Strip accents and lowercase: 'José' -> 'jose'. Emails are ASCII."""
    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", norm.lower())


def split_name(name: str) -> Optional[tuple]:
    """(first, last) from a free-text name, or None if unusable."""
    parts = [p for p in re.split(r"[\s,]+", (name or "").strip()) if p]
    if not parts:
        return None
    if len(parts) == 1:
        return (_ascii(parts[0]), "")
    first = _ascii(parts[0])
    last = _ascii(parts[-1])   # ignore middle names for the local part
    if not first and not last:
        return None
    return (first, last)


def generate(name: str, domain: str,
             preferred_shapes: Optional[dict] = None) -> List[Candidate]:
    """Ordered candidate list. A learned format for the domain floats the
    matching patterns to the top so we probe the likely one first."""
    from .patterns import local_shape

    split = split_name(name)
    if split is None:
        return []
    first, last = split
    domain = domain.strip().lower().lstrip("@")

    subs = {"first": first, "last": last,
            "f": first[:1], "l": last[:1]}

    seen = set()
    out: List[Candidate] = []
    for pattern in PATTERNS:
        try:
            local = pattern.format(**subs)
        except (KeyError, IndexError):
            continue
        local = local.strip(".-_")
        # Patterns needing a last name are useless for a single-word name.
        if not local or (not last and "{last}" in pattern):
            continue
        if local in seen:
            continue
        seen.add(local)
        email = "%s@%s" % (local, domain)
        out.append(Candidate(email, pattern, local_shape(local)))

    if preferred_shapes:
        # Stable sort: candidates whose shape matches a known format first,
        # heavier-evidence shapes ahead of lighter ones.
        def rank(c: Candidate) -> int:
            return -preferred_shapes.get(c.shape, 0)
        out.sort(key=rank)
    return out


async def _smtp_find(domain, mx_hosts, candidates, config):
    """Probe candidates on ONE connection, stop at the first real hit.

    Returns (email_or_None, status): 'found' (a 250), 'catch_all' (server
    accepts a fake, so nothing is knowable), or 'exhausted' (all rejected).
    Stopping early keeps a lookup to a couple of probes and avoids the
    'too many recipients' rate-limits some servers apply.
    """
    import asyncio

    from .smtp_check import (_POLICY_RE, _random_local, _read_response, _send)

    if not mx_hosts:
        return None, "unknown"

    # MX failover: try each listed server until one actually greets us, so a
    # dead primary MX doesn't cost us a verifiable domain.
    reader = writer = None
    for host in mx_hosts[:3]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, 25), config.timeout)
            code, _ = await _read_response(reader, config.timeout)
            if code == 220:
                break
            writer.close()
            reader = writer = None
        except Exception:  # noqa: BLE001 - try the next MX
            if writer is not None:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass
            reader = writer = None
    if writer is None:
        return None, "unknown"

    try:
        await _send(writer, "EHLO " + config.helo)
        code, _ = await _read_response(reader, config.timeout)
        if code != 250:
            await _send(writer, "HELO " + config.helo)
            code, _ = await _read_response(reader, config.timeout)
            if code != 250:
                return None, "blocked"
        await _send(writer, "MAIL FROM:<%s>" % config.mail_from)
        code, _ = await _read_response(reader, config.timeout)
        if code != 250:
            return None, "blocked"

        # Catch-all gate: if a fake is accepted, no candidate is distinguishable.
        await _send(writer, "RCPT TO:<%s@%s>" % (_random_local(), domain))
        code, detail = await _read_response(reader, config.timeout)
        if code in (250, 251):
            return None, "catch_all"
        if not 500 <= code < 600:
            return None, "unknown"      # greylist/temp -> can't trust this pass
        if _POLICY_RE.search(detail):
            # Fake refused for a reason about US (sender verify / block). Every
            # candidate will be refused the same way -- not a recipient verdict.
            return None, "blocked"

        for cand in candidates:
            await asyncio.sleep(config.per_domain_delay)
            await _send(writer, "RCPT TO:<%s>" % cand.email)
            code, _ = await _read_response(reader, config.timeout)
            if code in (250, 251):
                return cand.email, "found"    # first real hit -> done
        return None, "exhausted"
    except Exception:  # noqa: BLE001
        return None, "unknown"
    finally:
        if writer is not None:
            try:
                await _send(writer, "QUIT")
            except Exception:  # noqa: BLE001
                pass
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


async def find(name, domain, cache=None, use_microsoft=True, use_smtp=False,
               smtp_config=None, max_candidates=8) -> FindResult:
    from .providers import (GOOGLE_CONSUMER, GOOGLE_WORKSPACE, MICROSOFT,
                            NO_MAIL, SELF_HOSTED, classify)
    from .smtp_check import ProbeConfig

    domain = (domain or "").strip().lower().lstrip("@")
    result = FindResult(query=name, domain=domain)

    routes = await classify([domain])
    route = routes.get(domain)
    if route is None or route.route == NO_MAIL:
        result.status = "not_found"
        result.method = "no mail server for this domain"
        return result

    preferred = cache.load_shapes([domain]).get(domain) if cache else None
    candidates = generate(name, domain, preferred)[:max_candidates]
    if not candidates:
        result.status = "unknown"
        result.method = "couldn't read that name"
        return result
    result.candidates = [c.email for c in candidates]
    top = candidates[0]

    def as_guess(reason):
        result.email = top.email
        result.status = "guess"
        result.method = reason
        # More confident when the guess matches a format we've actually seen.
        result.confidence = 75 if preferred and top.shape in preferred else 45
        return result

    # -- Microsoft: HTTPS, no reputation cost --------------------------------
    if route.route == MICROSOFT and use_microsoft:
        from . import microsoft
        res = await microsoft.verify([c.email for c in candidates])
        by = {r.email.lower(): r for r in res}
        if any(r.status == "catch_all" for r in res):
            return as_guess("Microsoft tenant is opaque -- best pattern guess")
        for cand in candidates:
            r = by.get(cand.email.lower())
            if r and r.status == "valid":
                result.email, result.status = cand.email, "found"
                result.method, result.confidence = "Microsoft directory", 99
                result.tried = candidates.index(cand) + 1
                return result
        # None was a login identity -- may still be a deliverable alias.
        return as_guess("no Microsoft login match -- best pattern guess")

    # -- SMTP: verifiable self-hosted / Google -------------------------------
    if route.route in (SELF_HOSTED, GOOGLE_WORKSPACE, GOOGLE_CONSUMER) and use_smtp:
        email, status = await _smtp_find(
            domain, route.mx_hosts, candidates, smtp_config or ProbeConfig())
        if status == "found":
            result.email, result.status = email, "found"
            result.method, result.confidence = "SMTP verified", 98
            result.tried = result.candidates.index(email) + 1 if email in result.candidates else 0
            return result
        if status == "catch_all":
            return as_guess("catch-all domain -- best pattern guess")
        if status == "exhausted":
            result.status = "not_found"
            result.method = "no candidate accepted -- name may be wrong"
            return result
        return as_guess("couldn't verify (greylist/block) -- best pattern guess")

    # -- unverifiable (gateway, or SMTP off) ---------------------------------
    return as_guess("domain can't be probed -- best pattern guess")


async def find_many(pairs, cache=None, use_microsoft=True, use_smtp=False,
                    smtp_config=None, concurrency=5, progress=None):
    """Find emails for many (name, domain) pairs. Concurrency is kept low so
    the SMTP probing stays a gentle trickle, not a burst -- same reputation
    discipline as the verifier."""
    import asyncio

    semaphore = asyncio.Semaphore(concurrency)
    done = [0]
    total = len(pairs)

    async def one(name, domain):
        async with semaphore:
            try:
                r = await find(name, domain, cache=cache,
                               use_microsoft=use_microsoft, use_smtp=use_smtp,
                               smtp_config=smtp_config)
            except Exception:  # noqa: BLE001
                r = FindResult(query=name, domain=domain, status="unknown",
                               method="lookup failed")
            done[0] += 1
            if progress and (done[0] % 3 == 0 or done[0] == total):
                progress(done[0], total)
            return r

    return await asyncio.gather(*(one(n, d) for n, d in pairs))
