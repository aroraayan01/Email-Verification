"""Async MX/A resolution with gateway and provider fingerprinting.

Correctness note: a domain with no MX record is still mail-capable if it has
an A or AAAA record (RFC 5321 s5.1, implicit MX). Treating "no MX" as invalid
is one of the most common false-negative bugs in home-grown verifiers, so it
is handled explicitly here.
"""

import asyncio
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import dns.asyncresolver
import dns.exception
import dns.resolver

from .data import GATEWAY_PATTERNS, OPAQUE_PROVIDERS

# dns_status values
FOUND = "found"          # domain resolves and can accept mail
NO_MAIL = "no_mail"      # domain exists but has no mail route
NXDOMAIN = "nxdomain"    # domain does not exist -- the only hard invalid
ERROR = "error"          # timeout/servfail -- undecided, must be retried


@dataclass
class DomainResult:
    domain: str
    dns_status: str
    mail_capable: Optional[bool] = None
    mx_hosts: List[str] = field(default_factory=list)
    gateway: str = ""
    provider: str = ""
    note: str = ""


def fingerprint(mx_hosts: Sequence[str]) -> Tuple[str, str]:
    """Identify security appliances and opaque mailbox providers by MX host."""
    joined = " ".join(host.lower() for host in mx_hosts)
    gateway = ""
    provider = ""
    for fragment, name in GATEWAY_PATTERNS:
        if fragment in joined:
            gateway = name
            break
    for fragment, name in OPAQUE_PROVIDERS:
        if fragment in joined:
            provider = name
            break
    return gateway, provider


async def _resolve_one(domain: str, resolver: "dns.asyncresolver.Resolver",
                       semaphore: asyncio.Semaphore) -> DomainResult:
    async with semaphore:
        try:
            answer = await resolver.resolve(domain, "MX")
            hosts = sorted(
                str(rdata.exchange).rstrip(".").lower()
                for rdata in answer
                if str(rdata.exchange).rstrip(".") not in ("", ".")
            )
            if hosts:
                gateway, provider = fingerprint(hosts)
                return DomainResult(domain, FOUND, True, hosts, gateway, provider)
            # A single "." exchange is an explicit RFC 7505 null MX: the
            # domain is declaring that it accepts no mail at all.
            return DomainResult(domain, NO_MAIL, False, [], note="null MX")

        except dns.resolver.NXDOMAIN:
            return DomainResult(domain, NXDOMAIN, False, [], note="domain does not exist")

        except dns.resolver.NoAnswer:
            # No MX, but an A/AAAA record still makes the domain mail-capable.
            for rrtype in ("A", "AAAA"):
                try:
                    await resolver.resolve(domain, rrtype)
                    return DomainResult(domain, FOUND, True, [domain],
                                        note="implicit MX (A record)")
                except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                    continue
                except (dns.exception.Timeout, dns.resolver.NoNameservers):
                    return DomainResult(domain, ERROR, None, [],
                                        note="timeout resolving " + rrtype)
            return DomainResult(domain, NO_MAIL, False, [], note="no MX and no A record")

        except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
            # Undecided, never invalid.
            return DomainResult(domain, ERROR, None, [], note=str(exc)[:120])

        except Exception as exc:  # noqa: BLE001 - any resolver oddity is undecided
            return DomainResult(domain, ERROR, None, [], note=str(exc)[:120])


async def resolve_domains(domains: Sequence[str], concurrency: int = 25,
                          timeout: float = 10.0,
                          nameservers: Optional[List[str]] = None) -> List[DomainResult]:
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    if nameservers:
        resolver.nameservers = nameservers
    semaphore = asyncio.Semaphore(concurrency)
    return list(await asyncio.gather(
        *(_resolve_one(domain, resolver, semaphore) for domain in domains)
    ))
