"""Routing logic: decide locally where it is safe, escalate everywhere else.

The single rule this file exists to enforce:

    An address is only ever marked invalid on positive proof.
    Every other uncertainty routes to the paid tier.

False negatives cost a credit. False positives cost a client's sending
reputation. The asymmetry is deliberate and should stay that way.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cache import Cache
from .data import DISPOSABLE_DOMAINS
from .dns_check import ERROR, FOUND, NO_MAIL, NXDOMAIN, resolve_domains
from .normalize import HARD_INVALID, NEEDS_REVIEW, OK, parse, suggest_domain

# Buckets
RESOLVED = "resolved"       # decided locally, no credit spent
ESCALATE = "escalate"       # send to Clearout
REVIEW = "review"           # a human should look before anything happens


@dataclass
class Row:
    original: str
    canonical: str = ""
    domain: str = ""
    bucket: str = ESCALATE
    status: str = "unknown"
    reason: str = ""
    source: str = "prefilter"
    gateway: str = ""
    provider: str = ""
    is_role: bool = False
    suggestion: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class Report:
    total_input: int = 0
    unique: int = 0
    duplicates_removed: int = 0
    counts: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    gateways: Counter = field(default_factory=Counter)

    @property
    def escalated(self) -> int:
        return self.counts[ESCALATE]


def run_pipeline(raw_emails: List[str], cache: Cache, ttl_days: int = 90,
                 domain_ttl_days: int = 30, concurrency: int = 25,
                 timeout: float = 10.0, drop_role: bool = False,
                 disposable: Optional[set] = None,
                 nameservers: Optional[List[str]] = None) -> Tuple[List[Row], Report]:
    disposable_domains = disposable or DISPOSABLE_DOMAINS
    report = Report(total_input=len(raw_emails))

    # -- stage 1: parse and deduplicate -----------------------------------
    rows: List[Row] = []
    seen: Dict[str, Row] = {}
    for raw in raw_emails:
        parsed = parse(raw)
        row = Row(original=raw, canonical=parsed.canonical,
                  domain=parsed.domain, is_role=parsed.is_role)

        if parsed.status == HARD_INVALID:
            row.bucket, row.status, row.reason = RESOLVED, "invalid", parsed.reason
            rows.append(row)
            continue
        if parsed.status == NEEDS_REVIEW:
            row.bucket, row.status, row.reason = REVIEW, "unknown", parsed.reason
            rows.append(row)
            continue

        if parsed.canonical in seen:
            report.duplicates_removed += 1
            row.bucket, row.status = RESOLVED, "duplicate"
            row.reason = "duplicate of " + seen[parsed.canonical].original
            rows.append(row)
            continue

        seen[parsed.canonical] = row
        rows.append(row)

    report.unique = len(seen)
    pending = [row for row in rows if row.bucket == ESCALATE]

    # -- stage 2: cache ---------------------------------------------------
    still_pending: List[Row] = []
    for row in pending:
        cached = cache.lookup(row.canonical, ttl_days)
        if cached is None:
            still_pending.append(row)
            continue
        row.bucket = RESOLVED
        row.status = cached["status"]
        row.source = "cache:" + cached["source"]
        row.reason = "cached {0} from {1}".format(
            cached["status"], cached["checked_at"][:10])
    pending = still_pending

    # -- stage 3: disposable ----------------------------------------------
    still_pending = []
    for row in pending:
        if row.domain in disposable_domains:
            row.bucket, row.status = RESOLVED, "disposable"
            row.reason = "disposable domain"
        else:
            still_pending.append(row)
    pending = still_pending

    # -- stage 4: DNS -----------------------------------------------------
    domains = sorted({row.domain for row in pending})
    resolved: Dict[str, object] = {}
    to_query: List[str] = []
    for domain in domains:
        cached_domain = cache.get_domain(domain, domain_ttl_days)
        if cached_domain is None:
            to_query.append(domain)
        else:
            resolved[domain] = cached_domain

    if to_query:
        results = asyncio.run(resolve_domains(
            to_query, concurrency=concurrency, timeout=timeout,
            nameservers=nameservers))
        for result in results:
            cache.put_domain(result.domain, result.mail_capable,
                             result.mx_hosts, result.gateway, result.provider,
                             result.dns_status)
            resolved[result.domain] = result
        cache.commit()

    for row in pending:
        entry = resolved.get(row.domain)
        if entry is None:
            row.reason = "domain not resolved"
            continue

        if hasattr(entry, "dns_status"):           # fresh DomainResult
            dns_status = entry.dns_status
            gateway, provider, note = entry.gateway, entry.provider, entry.note
        else:                                      # cached sqlite Row
            dns_status = entry["dns_status"]
            gateway, provider, note = entry["gateway"], entry["provider"], ""

        row.gateway, row.provider = gateway, provider

        if dns_status == NXDOMAIN:
            # The only DNS outcome that proves an address cannot exist.
            row.bucket, row.status = RESOLVED, "invalid"
            row.reason = "domain does not exist (NXDOMAIN)"
            suggestion = suggest_domain(row.domain)
            if suggestion:
                row.suggestion = suggestion
                row.bucket, row.reason = REVIEW, (
                    "NXDOMAIN, possible typo of " + suggestion)
        elif dns_status == NO_MAIL:
            row.bucket, row.status = RESOLVED, "invalid"
            row.reason = note or "domain accepts no mail"
        elif dns_status == ERROR:
            row.bucket, row.status = ESCALATE, "unknown"
            row.reason = "DNS undecided, escalating"
        elif dns_status == FOUND:
            row.bucket, row.status = ESCALATE, "unknown"
            if gateway:
                row.reason = "security gateway ({0})".format(gateway)
            elif provider:
                row.reason = "opaque provider ({0})".format(provider)
            else:
                row.reason = "mail-capable, needs mailbox check"
            # A domain that resolves but sits one or two edits from a major
            # provider is usually a typosquat, and those are prime spam-trap
            # territory. Escalating it isn't enough -- Clearout will happily
            # confirm the mailbox exists.
            suggestion = suggest_domain(row.domain)
            if suggestion:
                row.suggestion = suggestion
                row.bucket = REVIEW
                row.reason = "resolves, but looks like a typo of " + suggestion

    # -- stage 5: role accounts -------------------------------------------
    for row in rows:
        if row.is_role and row.bucket == ESCALATE and drop_role:
            row.bucket, row.status = RESOLVED, "role"
            row.reason = "role account (dropped by --drop-role)"

    # -- tally ------------------------------------------------------------
    for row in rows:
        report.counts[row.bucket] += 1
        report.reasons[row.reason or row.status] += 1
        if row.gateway:
            report.gateways[row.gateway] += 1

    # Persist everything we decided ourselves so the next run is cheaper.
    #
    # 'duplicate' is a processing artifact, not a verdict about the address --
    # and because a duplicate shares its canonical key with the original,
    # caching it would overwrite the original's real verdict and silently drop
    # the address on a later run.
    for row in rows:
        if (row.bucket == RESOLVED and row.canonical
                and row.source == "prefilter" and row.status != "duplicate"):
            cache.put(row.canonical, row.original, "prefilter", row.status,
                      row.reason)
    cache.commit()

    return rows, report
