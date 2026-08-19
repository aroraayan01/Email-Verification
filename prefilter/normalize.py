"""Address parsing, canonicalisation and typo suggestion.

The canonical form exists for two purposes only: deduplication and cache
keying. The original string is always what gets written back out.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .data import (
    COMMON_DOMAINS,
    DOT_INSENSITIVE,
    GOOGLEMAIL_ALIASES,
    PLUS_ADDRESSING,
    ROLE_LOCALS,
)

# RFC 5322 is famously permissive. This accepts everything real mail systems
# actually issue and rejects only the unambiguous garbage -- anything exotic
# but conceivably legal is sent to review rather than called invalid.
_LOCAL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)
_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)

# Verdicts that mean "we are certain this cannot receive mail".
HARD_INVALID = "invalid"
# Verdicts that mean "we could not decide" -- these escalate, never drop.
NEEDS_REVIEW = "review"
OK = "ok"


@dataclass
class Parsed:
    original: str
    status: str           # OK | HARD_INVALID | NEEDS_REVIEW
    reason: str = ""
    local: str = ""
    domain: str = ""
    canonical: str = ""
    is_role: bool = False


def _strip_wrapping(raw: str) -> str:
    s = raw.strip()
    # Handle "Name <addr@example.com>" and bare <addr@example.com>.
    if s.endswith(">") and "<" in s:
        s = s[s.rindex("<") + 1:-1].strip()
    return s.strip().strip(",;")


def _to_ascii_domain(domain: str) -> Optional[str]:
    """Punycode an IDN domain. Returns None if it cannot be encoded."""
    try:
        return domain.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None


def parse(raw: str) -> Parsed:
    """Parse and canonicalise a single address.

    Only structural impossibilities are called invalid here. Everything the
    parser merely dislikes goes to review, because a false 'invalid' is the
    one error this pipeline must never make.
    """
    original = (raw or "").strip()
    if not original:
        return Parsed(original, HARD_INVALID, "empty")

    s = _strip_wrapping(original)

    if s.count("@") == 0:
        return Parsed(original, HARD_INVALID, "no @ sign")
    if s.count("@") > 1:
        # Quoted local parts may legally contain @, so this is review, not
        # invalid -- but it is almost always a mangled cell.
        if s.startswith('"'):
            return Parsed(original, NEEDS_REVIEW, "quoted local part")
        return Parsed(original, HARD_INVALID, "multiple @ signs")

    local, domain = s.rsplit("@", 1)
    if not local:
        return Parsed(original, HARD_INVALID, "empty local part")
    if not domain:
        return Parsed(original, HARD_INVALID, "empty domain")
    if len(local) > 64:
        return Parsed(original, HARD_INVALID, "local part over 64 chars")

    domain = domain.lower().rstrip(".")

    if not domain.isascii():
        ascii_domain = _to_ascii_domain(domain)
        if ascii_domain is None:
            return Parsed(original, NEEDS_REVIEW, "undecodable IDN domain")
        domain = ascii_domain

    if not _DOMAIN_RE.match(domain):
        # No dot at all, or an illegal shape. A dotless domain cannot be
        # publicly routable.
        if "." not in domain:
            return Parsed(original, HARD_INVALID, "domain has no TLD")
        return Parsed(original, NEEDS_REVIEW, "unusual domain syntax")

    if not _LOCAL_RE.match(local):
        if local.startswith('"') and local.endswith('"'):
            return Parsed(original, NEEDS_REVIEW, "quoted local part")
        return Parsed(original, NEEDS_REVIEW, "unusual local part syntax")

    canonical_domain = GOOGLEMAIL_ALIASES.get(domain, domain)
    canonical_local = local.lower()

    if canonical_domain in PLUS_ADDRESSING and "+" in canonical_local:
        canonical_local = canonical_local.split("+", 1)[0]
    if canonical_domain in DOT_INSENSITIVE:
        canonical_local = canonical_local.replace(".", "")

    if not canonical_local:
        return Parsed(original, HARD_INVALID, "local part empty after normalising")

    return Parsed(
        original=original,
        status=OK,
        local=local,
        domain=domain,
        canonical="{0}@{1}".format(canonical_local, canonical_domain),
        is_role=local.lower().split("+", 1)[0] in ROLE_LOCALS,
    )


def _levenshtein(a: str, b: str, cutoff: int = 3) -> int:
    """Plain DP edit distance. Lists are tiny, so this needs no dependency."""
    if abs(len(a) - len(b)) > cutoff:
        return cutoff + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def suggest_domain(domain: str) -> Optional[str]:
    """Nearest common domain within edit distance 2, or None.

    Suggestion only. Nothing downstream rewrites an address on this basis --
    with zero tolerance for false positives, a human confirms every change.
    """
    if domain in COMMON_DOMAINS:
        return None
    best, best_distance = None, 3
    for candidate in COMMON_DOMAINS:
        distance = _levenshtein(domain, candidate)
        if distance < best_distance:
            best, best_distance = candidate, distance
    return best if best_distance <= 2 else None
