"""The engine: route every address to the tier that can prove something about it.

Tiers, cheapest and most certain first. An address leaves the pipeline the
moment a tier can prove its verdict; anything unproven falls through.

    0. cache      -- a verdict already paid for
    1. local      -- syntax, dedupe, DNS. Proves 'invalid' for dead domains.
    2. microsoft  -- HTTPS GetCredentialType. Proves 'valid'. No SMTP, no IP
                     reputation, runs anywhere. ~48% of a B2B list.
    3. smtp       -- per-domain baseline probing. Needs a clean sending IP,
                     so it runs on the server, never a residential connection.
    4. vendor     -- everything still unproven goes to Clearout.

The governing rule, unchanged from tier 0 to tier 4: an address is only marked
invalid on positive proof. When this engine is wrong it must be wrong in the
direction that costs a credit, never the direction that bounces on a client's
sending domain.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .cache import Cache
from .data import DISPOSABLE_DOMAINS
from .normalize import HARD_INVALID, NEEDS_REVIEW, OK, parse, suggest_domain
from .providers import (GATEWAY, GOOGLE_CONSUMER, GOOGLE_WORKSPACE, MICROSOFT,
                        NO_MAIL, SELF_HOSTED, UNRESOLVED, classify)

# Terminal dispositions
SIPHONED = "siphoned"     # proven -- costs no credit
TO_VENDOR = "to_vendor"   # unproven -- send to Clearout
REVIEW = "review"         # a human should look


@dataclass
class Verdict:
    email: str
    canonical: str = ""
    domain: str = ""
    status: str = "unknown"      # valid | invalid | catch_all | unknown
    disposition: str = TO_VENDOR
    tier: str = ""
    reason: str = ""
    route: str = ""
    suggestion: str = ""
    confidence: Optional[int] = None   # pattern tier only: 0..100


@dataclass
class EngineReport:
    total: int = 0
    unique: int = 0
    by_disposition: Counter = field(default_factory=Counter)
    by_tier: Counter = field(default_factory=Counter)
    by_route: Counter = field(default_factory=Counter)
    by_status: Counter = field(default_factory=Counter)

    @property
    def siphoned(self) -> int:
        return self.by_disposition[SIPHONED]

    @property
    def billable(self) -> int:
        return self.by_disposition[TO_VENDOR]


def _seed(emails: Sequence[str]) -> Tuple[List[Verdict], Dict[str, Verdict]]:
    """Parse, dedupe, and settle anything structurally decidable."""
    verdicts: List[Verdict] = []
    live: Dict[str, Verdict] = {}
    for raw in emails:
        parsed = parse(raw)
        verdict = Verdict(email=raw, canonical=parsed.canonical,
                          domain=parsed.domain)
        if parsed.status == HARD_INVALID:
            verdict.status, verdict.disposition = "invalid", SIPHONED
            verdict.tier, verdict.reason = "local", parsed.reason
        elif parsed.status == NEEDS_REVIEW:
            verdict.status, verdict.disposition = "unknown", REVIEW
            verdict.tier, verdict.reason = "local", parsed.reason
        elif parsed.canonical in live:
            verdict.status, verdict.disposition = "duplicate", SIPHONED
            verdict.tier = "local"
            verdict.reason = "duplicate of " + live[parsed.canonical].email
        else:
            live[parsed.canonical] = verdict
        verdicts.append(verdict)
    return verdicts, live


async def run(emails: Sequence[str], cache: Optional[Cache] = None,
              ttl_days: int = 90, use_microsoft: bool = True,
              use_smtp: bool = False, smtp_config=None,
              nameservers: Optional[List[str]] = None,
              use_patterns: bool = True, pattern_threshold: int = 0,
              log=print, on_progress=None
              ) -> Tuple[List[Verdict], EngineReport]:
    """Run every tier. on_progress(stage, done, total) drives the web UI."""
    def progress(stage, done=0, total=0):
        if on_progress:
            on_progress(stage, done, total)
    report = EngineReport(total=len(emails))
    verdicts, live = _seed(emails)
    report.unique = len(live)
    log("  parsed %d rows -> %d unique addresses" % (len(emails), len(live)))

    pending = [v for v in live.values() if v.disposition == TO_VENDOR]

    # -- tier 0: verdicts already paid for --------------------------------
    if cache is not None:
        still = []
        for verdict in pending:
            hit = cache.lookup(verdict.canonical, ttl_days)
            if hit is None:
                still.append(verdict)
            else:
                verdict.status = hit["status"]
                verdict.disposition = SIPHONED
                verdict.tier = "cache"
                verdict.reason = "cached %s (%s)" % (hit["status"],
                                                     hit["checked_at"][:10])
        if len(pending) != len(still):
            log("  tier0 cache      : %d resolved" % (len(pending) - len(still)))
        pending = still

    # -- tier 1: disposable -----------------------------------------------
    still = []
    for verdict in pending:
        if verdict.domain in DISPOSABLE_DOMAINS:
            verdict.status, verdict.disposition = "disposable", SIPHONED
            verdict.tier, verdict.reason = "local", "disposable domain"
        else:
            still.append(verdict)
    pending = still

    # -- tier 1: DNS + provider routing ------------------------------------
    domains = sorted({v.domain for v in pending})
    progress("Looking up mail servers", 0, len(domains))
    routes = await classify(domains, nameservers=nameservers) if domains else {}
    progress("Looking up mail servers", len(domains), len(domains))
    for verdict in pending:
        route = routes.get(verdict.domain)
        verdict.route = route.route if route else UNRESOLVED
        report.by_route[verdict.route] += 1

    still = []
    for verdict in pending:
        if verdict.route == NO_MAIL:
            # The only thing DNS can prove: this domain accepts no mail at all.
            verdict.status, verdict.disposition = "invalid", SIPHONED
            verdict.tier = "local"
            verdict.reason = (routes[verdict.domain].detail
                              if verdict.domain in routes else "no mail route")
            hint = suggest_domain(verdict.domain)
            if hint:
                verdict.suggestion = hint
                verdict.disposition, verdict.reason = REVIEW, (
                    "dead domain, possible typo of " + hint)
        else:
            still.append(verdict)
    resolved_dns = len(pending) - len(still)
    if resolved_dns:
        log("  tier1 dns        : %d resolved" % resolved_dns)
    pending = still

    # -- tier 2: Microsoft over HTTPS --------------------------------------
    # Also try GATEWAY domains here: a Proofpoint/Mimecast MX is only an inbound
    # filter -- the mailboxes behind it are often Microsoft 365, and the HTTPS
    # directory check ignores the gateway entirely. Non-Microsoft domains just
    # come back unknown (no false positives), so this is free upside.
    ms_targets = [v for v in pending if v.route in (MICROSOFT, GATEWAY)]
    if use_microsoft and ms_targets:
        from . import microsoft
        log("  tier2 microsoft  : querying %d addresses ..." % len(ms_targets))
        by_email = {v.canonical: v for v in ms_targets}
        progress("Checking Microsoft accounts", 0, len(ms_targets))
        results = await microsoft.verify(
            [v.canonical for v in ms_targets],
            progress=lambda d, t: progress("Checking Microsoft accounts", d, t))
        proven = 0
        for result in results:
            verdict = by_email.get(result.email)
            if verdict is None:
                continue
            if result.status == "valid":
                # Proven: Microsoft recognises this as a real login identity.
                verdict.status, verdict.disposition = "valid", SIPHONED
                verdict.tier, verdict.reason = "microsoft", result.detail
                proven += 1
            else:
                # catch_all / unknown -- unproven, so it stays billable, but
                # surface catch_all so the user sees why it can't be settled.
                if result.status == "catch_all":
                    verdict.status = "catch_all"
                verdict.reason = result.detail
        log("  tier2 microsoft  : %d proven valid (%d fell through)"
            % (proven, len(ms_targets) - proven))
        pending = [v for v in pending if v.disposition == TO_VENDOR]

    # -- tier 3: SMTP, only from an IP that is allowed to ask ---------------
    # Google Workspace is included: Google's MX answers RCPT like consumer
    # Gmail does, so SMTP is the only lever that reaches it (Google has no
    # directory endpoint to query). smtp_check paces Google hosts gently.
    smtp_targets = [v for v in pending
                    if v.route in (SELF_HOSTED, GOOGLE_CONSUMER, GOOGLE_WORKSPACE)]
    if use_smtp and smtp_targets:
        from .smtp_check import ProbeConfig, probe_all
        log("  tier3 smtp       : probing %d addresses ..." % len(smtp_targets))
        by_email = {v.canonical: v for v in smtp_targets}
        results = await probe_all([v.canonical for v in smtp_targets],
                                  smtp_config or ProbeConfig())
        proven = 0
        for result in results:
            verdict = by_email.get(result.email)
            if verdict is None:
                continue
            if result.status in ("valid", "invalid"):
                verdict.status, verdict.disposition = result.status, SIPHONED
                verdict.tier = "smtp"
                verdict.reason = "%s (%s)" % (result.detail, result.mx)
                proven += 1
            else:
                # catch_all is a real finding worth surfacing (not just
                # 'unknown'); it still goes to the vendor, but the user sees
                # WHY it's unprovable.
                if result.status == "catch_all":
                    verdict.status = "catch_all"
                verdict.reason = result.detail
        log("  tier3 smtp       : %d proven (%d fell through)"
            % (proven, len(smtp_targets) - proven))

    # -- tier 3.5: pattern scoring for what no probe can settle ------------
    # Everything still unproven -- catch-alls, Google Workspace, gateways,
    # Microsoft aliases. Certainty is impossible here, so this produces a
    # confidence, clearly labelled, never a plain 'valid'.
    if use_patterns:
        from . import patterns

        pending = [v for v in verdicts if v.disposition == TO_VENDOR
                   and v.canonical]
        if pending:
            # Learn from everything proven valid this run...
            proven_valid = [v.canonical for v in verdicts
                            if v.status == "valid" and v.canonical]
            profiles = patterns.build_profiles(proven_valid)

            # ...and merge in formats learned on every previous list.
            if cache is not None:
                domains = {v.domain for v in pending}
                for domain, shapes in cache.load_shapes(domains).items():
                    prof = profiles.setdefault(domain, patterns.DomainProfile())
                    for shape, count in shapes.items():
                        prof.shapes[shape] = prof.shapes.get(shape, 0) + count

            scored = 0
            promoted = 0
            for verdict in pending:
                profile = profiles.get(verdict.domain)
                result = patterns.score_address(verdict.canonical, profile)
                verdict.confidence = result.confidence
                scored += 1
                # Advisory by default. Only cross into 'siphoned' when the user
                # sets a confidence line and the address clears it.
                if (pattern_threshold and result.label == "likely_valid"
                        and result.confidence >= pattern_threshold):
                    verdict.status = "likely_valid"
                    verdict.disposition = SIPHONED
                    verdict.tier = "pattern"
                    verdict.reason = "%d%% confident: %s" % (
                        result.confidence, result.reason)
                    promoted += 1
                elif verdict.disposition == TO_VENDOR:
                    hint = "%d%% likely real (guess only)" % result.confidence
                    if verdict.status == "catch_all":
                        # Keep the catch_all label; the % is just a text hint.
                        verdict.reason = ("catch-all domain -- accepts every "
                                          "address, can't be confirmed; %s" % hint)
                    else:
                        verdict.reason = "%s -- %s" % (hint, result.reason)
            log("  tier3.5 pattern  : scored %d, %d promoted at >=%d%%"
                % (scored, promoted, pattern_threshold or 101))

            # Persist this run's proven formats for next time.
            if cache is not None:
                for email in proven_valid:
                    local, domain = email.rsplit("@", 1)
                    cache.learn_shape(domain, patterns.local_shape(local))
                cache.commit()

    # -- tally --------------------------------------------------------------
    for verdict in verdicts:
        if verdict.disposition == TO_VENDOR and not verdict.reason:
            verdict.reason = "unproven -- %s" % (verdict.route or "unrouted")
        report.by_disposition[verdict.disposition] += 1
        report.by_status[verdict.status] += 1
        if verdict.tier:
            report.by_tier[verdict.tier] += 1

    if cache is not None:
        for verdict in verdicts:
            if (verdict.disposition == SIPHONED and verdict.canonical
                    and verdict.tier not in ("cache", "")
                    and verdict.status != "duplicate"):
                cache.put(verdict.canonical, verdict.email, verdict.tier,
                          verdict.status, verdict.reason)
        cache.commit()

    return verdicts, report
