"""Confidence scoring for addresses no probe can settle.

On a catch-all or opaque domain the server answers "yes" to everything, so
certainty is impossible. What remains are *patterns*, and this module reads the
ones that actually carry signal:

  1. Format.  A company almost always issues addresses in one shape --
     first.last, flast, first, etc. Learn that shape from the addresses we have
     already PROVEN valid, then score a new address by whether it fits.
  2. Shape of the local part.  A human name decomposes into pronounceable
     tokens; a spam-trap or scrape artefact often does not. "john.smith" reads
     as a person, "xk9zqp" does not -- and that judgement needs no per-domain
     history at all.
  3. Role accounts.  info@, sales@, support@ are near-universally deliverable,
     catch-all or not.

The output is a probability, never a proof. It is deliberately kept apart from
the proven tiers: pattern results are labelled `likely_valid` / `likely_invalid`
and never silently become a plain `valid`. The user chooses the confidence line
they are willing to send at.
"""

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .data import ROLE_LOCALS

# Bigram log-probabilities would be ideal; a compact, dependency-free proxy is
# good enough to separate names from noise. English letter frequency, used to
# weight an entropy estimate.
_VOWELS = set("aeiou")
_COMMON_BIGRAMS = {
    "th", "he", "in", "er", "an", "re", "on", "at", "en", "nd", "ti", "es",
    "or", "te", "of", "ed", "is", "it", "al", "ar", "st", "to", "nt", "ng",
    "se", "ha", "as", "ou", "io", "le", "ve", "co", "me", "de", "hi", "ri",
    "ro", "ic", "ne", "ea", "ra", "ce", "li", "ch", "ll", "be", "ma", "si",
    "om", "ur", "ca", "el", "ta", "la", "ns", "di", "fo", "ho", "pe", "ec",
    "pr", "ni", "me", "ns", "na", "an", "lo", " mi", "sm", "jo", "wi", "da",
}

_SEP_RE = re.compile(r"[._\-+]")


def local_shape(local: str) -> str:
    """Structural template of a local part, separators preserved.

    john.smith -> a.a    jsmith -> a    john.smith99 -> a.an    j_s -> a_a
    Length is intentionally ignored so j.smith and john.smith share a shape --
    the format family is what matters, not the individual's name length.
    """
    parts = _SEP_RE.split(local)
    seps = _SEP_RE.findall(local)
    tokens = []
    for part in parts:
        if not part:
            tokens.append("")
            continue
        has_alpha = any(c.isalpha() for c in part)
        has_digit = any(c.isdigit() for c in part)
        if has_alpha and has_digit:
            tokens.append("an")
        elif has_digit:
            tokens.append("n")
        else:
            tokens.append("a")
    out = tokens[0] if tokens else ""
    for sep, tok in zip(seps, tokens[1:]):
        out += sep + tok
    return out


def gibberish_score(local: str) -> float:
    """0.0 = reads like a name/word, 1.0 = reads like random noise.

    Combines vowel ratio, longest consonant run, and how many adjacent letter
    pairs are common English bigrams. None of these alone is reliable; together
    they separate 'katherine' from 'xkzqpw' well enough to be useful.
    """
    letters = [c for c in local.lower() if c.isalpha()]
    if len(letters) < 4:
        return 0.0  # too short to judge; give it the benefit of the doubt
    text = "".join(letters)

    vowels = sum(1 for c in letters if c in _VOWELS)
    vowel_ratio = vowels / len(letters)

    longest_cons = run = 0
    for c in letters:
        if c in _VOWELS:
            run = 0
        else:
            run += 1
            longest_cons = max(longest_cons, run)

    bigrams = [text[i:i + 2] for i in range(len(text) - 1)]
    good = sum(1 for b in bigrams if b in _COMMON_BIGRAMS)
    bigram_ratio = good / max(1, len(bigrams))

    score = 0.0
    # No vowels in a longish string is the strongest tell.
    if vowel_ratio == 0:
        score += 0.6
    elif vowel_ratio < 0.2 or vowel_ratio > 0.8:
        score += 0.3
    if longest_cons >= 5:
        score += 0.3
    elif longest_cons == 4:
        score += 0.15
    if bigram_ratio < 0.1:
        score += 0.3
    elif bigram_ratio < 0.25:
        score += 0.15
    return min(1.0, score)


@dataclass
class Score:
    email: str
    confidence: int          # 0..100, chance the address is real
    label: str               # likely_valid | likely_invalid | unknown
    reason: str = ""


class DomainProfile:
    """What we know about how one domain forms its addresses."""

    def __init__(self, shapes: Optional[Dict[str, int]] = None):
        self.shapes: Dict[str, int] = dict(shapes or {})

    def learn(self, local: str) -> None:
        self.shapes[local_shape(local)] = self.shapes.get(local_shape(local), 0) + 1

    @property
    def evidence(self) -> int:
        return sum(self.shapes.values())

    def fits(self, local: str) -> Optional[bool]:
        """True/False if we have enough evidence to judge, else None."""
        if self.evidence < 1:
            return None
        return local_shape(local) in self.shapes


def score_address(email: str, profile: Optional[DomainProfile]) -> Score:
    if "@" not in email:
        return Score(email, 0, "likely_invalid", "not an address")
    local = email.rsplit("@", 1)[0].lower()

    base = local.split("+", 1)[0]
    if base in ROLE_LOCALS:
        return Score(email, 90, "likely_valid", "role account, near-always deliverable")

    gib = gibberish_score(local)
    fit = profile.fits(local) if profile else None

    # Start from the structural read of the local part.
    confidence = int(round((1.0 - gib) * 60)) + 20   # 20..80 before format
    reasons = []
    if gib >= 0.6:
        reasons.append("local part looks random")
    elif gib <= 0.15:
        reasons.append("reads like a real name")

    # Format evidence, when we have any, dominates.
    if fit is True:
        confidence = min(95, confidence + 25)
        reasons.append("matches this domain's address format (%d known)"
                       % profile.evidence)
    elif fit is False:
        confidence = max(5, confidence - 35)
        reasons.append("does not match this domain's format (%d known)"
                       % profile.evidence)

    confidence = max(0, min(100, confidence))
    if confidence >= 70:
        label = "likely_valid"
    elif confidence <= 35:
        label = "likely_invalid"
    else:
        label = "unknown"
    return Score(email, confidence, label, "; ".join(reasons) or "structural guess")


def build_profiles(known_valid: Iterable[str]) -> Dict[str, DomainProfile]:
    """Learn per-domain formats from a pool of proven-valid addresses."""
    profiles: Dict[str, DomainProfile] = {}
    for email in known_valid:
        if "@" not in email:
            continue
        local, domain = email.rsplit("@", 1)
        profiles.setdefault(domain.lower(), DomainProfile()).learn(local.lower())
    return profiles
