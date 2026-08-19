"""Route each domain to the verification technique that can actually answer it.

The central idea of the whole engine: there is no single method that works
everywhere. SMTP is blind to Microsoft and Google tenants; Microsoft leaks
existence over HTTPS but says nothing about anyone else; catch-all domains
answer to no one. So classify the domain first, then pick the tool.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import dns.asyncresolver
import dns.exception
import dns.resolver

# Routes
MICROSOFT = "microsoft"       # login.microsoftonline.com GetCredentialType
GOOGLE_CONSUMER = "gmail"     # SMTP, but paced -- gmail.com is not catch-all
GOOGLE_WORKSPACE = "gsuite"   # usually opaque
GATEWAY = "gateway"           # Proofpoint/Mimecast/etc in front of the real MX
SELF_HOSTED = "smtp"          # ordinary server -- SMTP baseline probing works
NO_MAIL = "no_mail"           # NXDOMAIN / null MX
UNRESOLVED = "unresolved"

# MX fingerprints. Order matters: gateways are checked before the big
# providers, because a Proofpoint-fronted Microsoft tenant answers as
# Proofpoint at the SMTP layer.
GATEWAY_MX = [
    ("pphosted.com", "Proofpoint"), ("ppe-hosted.com", "Proofpoint"),
    ("mimecast.com", "Mimecast"), ("mimecast.co.za", "Mimecast"),
    ("barracudanetworks.com", "Barracuda"), ("barracuda.com", "Barracuda"),
    ("iphmx.com", "Cisco IronPort"), ("sophos.com", "Sophos"),
    ("mailcontrol.com", "Forcepoint"), ("trendmicro.com", "Trend Micro"),
    ("messagelabs.com", "Symantec"), ("securence.com", "Securence"),
    ("spamtitan.com", "SpamTitan"), ("hornetsecurity.com", "Hornetsecurity"),
    ("antispamcloud.com", "SpamExperts"), ("mailanyone.net", "Fusemail"),
    ("emailsrvr.com", "Rackspace"), ("mailguard.com.au", "MailGuard"),
    ("forcepoint.net", "Forcepoint"), ("qq.com", "Tencent"),
]

MICROSOFT_MX = ("protection.outlook.com", "mail.protection.outlook.com",
                "outlook.com", "hotmail.com", "office365.us")

GOOGLE_MX = ("google.com", "googlemail.com", "aspmx.l.google.com",
             "googlemail.l.google.com")

CONSUMER_MICROSOFT = {"outlook.com", "hotmail.com", "live.com", "msn.com",
                      "hotmail.co.uk", "outlook.in", "live.co.uk",
                      "hotmail.fr", "hotmail.it", "hotmail.de"}
CONSUMER_GOOGLE = {"gmail.com", "googlemail.com"}


@dataclass
class DomainRoute:
    domain: str
    route: str
    mx_hosts: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def reachable(self) -> bool:
        return self.route in (MICROSOFT, GOOGLE_CONSUMER, SELF_HOSTED, NO_MAIL)


def route_for(domain: str, mx_hosts: Sequence[str]) -> DomainRoute:
    domain = domain.lower()
    joined = " ".join(h.lower() for h in mx_hosts)

    if domain in CONSUMER_GOOGLE:
        return DomainRoute(domain, GOOGLE_CONSUMER, list(mx_hosts),
                           "consumer Gmail -- honest yes/no over SMTP, needs pacing")
    if domain in CONSUMER_MICROSOFT:
        return DomainRoute(domain, MICROSOFT, list(mx_hosts),
                           "consumer Microsoft -- GetCredentialType")

    # A gateway hides whatever is behind it, so it wins over provider match.
    for fragment, name in GATEWAY_MX:
        if fragment in joined:
            return DomainRoute(domain, GATEWAY, list(mx_hosts), name)

    if any(f in joined for f in MICROSOFT_MX):
        return DomainRoute(domain, MICROSOFT, list(mx_hosts),
                           "Microsoft 365 tenant -- GetCredentialType")
    if any(f in joined for f in GOOGLE_MX):
        return DomainRoute(domain, GOOGLE_WORKSPACE, list(mx_hosts),
                           "Google Workspace -- usually opaque")
    if not mx_hosts:
        return DomainRoute(domain, NO_MAIL, [], "no MX and no A record")
    return DomainRoute(domain, SELF_HOSTED, list(mx_hosts),
                       "ordinary mail server -- SMTP baseline probing")


async def _try_resolve(domain: str, resolver) -> Optional[DomainRoute]:
    """One attempt against one resolver. None means 'retry elsewhere'."""
    try:
        answer = await resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".").lower()
                 for r in sorted(answer, key=lambda r: r.preference)]
        hosts = [h for h in hosts if h and h != "."]
        if not hosts:
            return DomainRoute(domain, NO_MAIL, [], "null MX (RFC 7505)")
        return route_for(domain, hosts)
    except dns.resolver.NXDOMAIN:
        return DomainRoute(domain, NO_MAIL, [], "NXDOMAIN")
    except dns.resolver.NoAnswer:
        try:
            await resolver.resolve(domain, "A")
            return route_for(domain, [domain])
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return DomainRoute(domain, NO_MAIL, [], "no MX and no A record")
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


async def _resolve(domain: str, resolvers, semaphore) -> DomainRoute:
    """Try each resolver in turn. A DNS timeout is lost coverage, not a verdict.

    On the first pass 10% of a real list came back unresolved purely because a
    single resolver timed out, so every domain gets a second and third chance
    on independent public resolvers before we give up on it.
    """
    async with semaphore:
        for resolver in resolvers:
            route = await _try_resolve(domain, resolver)
            if route is not None:
                return route
        return DomainRoute(domain, UNRESOLVED, [],
                           "no resolver could answer after %d tries" % len(resolvers))


def _build_resolvers(timeout: float, nameservers: Optional[List[str]]):
    """System resolver first, then independent public ones as fallback."""
    pools = [None]
    pools.extend([[ns] for ns in (nameservers or
                                  ["1.1.1.1", "8.8.8.8", "9.9.9.9"])])
    built = []
    for servers in pools:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        if servers:
            resolver.nameservers = servers
        built.append(resolver)
    return built


async def classify(domains: Sequence[str], concurrency: int = 25,
                   timeout: float = 15.0,
                   nameservers: Optional[List[str]] = None
                   ) -> Dict[str, DomainRoute]:
    resolvers = _build_resolvers(timeout, nameservers)
    semaphore = asyncio.Semaphore(concurrency)
    routes = await asyncio.gather(
        *(_resolve(d, resolvers, semaphore) for d in domains))
    return {r.domain: r for r in routes}


def summarise(routes: Dict[str, DomainRoute],
              addresses: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for address in addresses:
        if "@" not in address:
            continue
        route = routes.get(address.rsplit("@", 1)[1].lower())
        counts[route.route if route else UNRESOLVED] += 1
    return dict(counts)
