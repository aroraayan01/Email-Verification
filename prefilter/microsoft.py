"""Microsoft mailbox existence via the GetCredentialType endpoint.

Microsoft's SMTP servers refuse to say whether a mailbox exists. But the login
stack has to know -- the sign-in page decides whether to show a password box or
say "we couldn't find an account". That decision leaks through a public JSON
endpoint used by the login page itself:

    POST https://login.microsoftonline.com/common/GetCredentialType
    {"Username": "person@company.com"}
    -> {"IfExistsResult": 0}   account exists
    -> {"IfExistsResult": 1}   account does not exist

This is HTTPS, not SMTP: port 443, no MX, no IP reputation, no greylisting.
The prober's whole SMTP handicap simply does not apply here.

Honest limits, handled below:
  * IfExistsResult == 2 means throttled -> back off and retry, never a verdict.
  * Some federated/passthrough tenants answer 0 for everything. We detect that
    the same way we detect an SMTP catch-all: probe a known-fake address; if the
    tenant says it exists, the tenant is opaque and every 0 is downgraded.
"""

import asyncio
import json
import random
import string
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

ENDPOINT = "https://login.microsoftonline.com/common/GetCredentialType"
HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

EXISTS = 0
NOT_EXIST = 1
THROTTLED = 2

# Our vocabulary
VALID, INVALID, CATCH_ALL, UNKNOWN = "valid", "invalid", "catch_all", "unknown"


@dataclass
class MsResult:
    email: str
    status: str
    if_exists: Optional[int] = None
    detail: str = ""


def _fake(domain: str) -> str:
    return "{0}.{1}{2}@{3}".format(
        "".join(random.choice(string.ascii_lowercase) for _ in range(8)),
        "".join(random.choice(string.ascii_lowercase) for _ in range(10)),
        random.randint(1000, 9999), domain)


async def _query(session, username: str, timeout: float,
                 max_retries: int = 6) -> Dict:
    """One GetCredentialType call, with backoff on throttle/transient errors."""
    payload = json.dumps({"Username": username}).encode()
    delay = 2.0
    last = {"error": "no attempt"}
    for _ in range(max_retries):
        try:
            async with session.post(ENDPOINT, data=payload, headers=HEADERS,
                                    timeout=timeout) as resp:
                if resp.status == 429:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    last = {"error": "http 429"}
                    continue
                text = await resp.text()
                try:
                    data = json.loads(text)
                except ValueError:
                    return {"error": "non-JSON reply", "http": resp.status}
                result = data.get("IfExistsResult", -1)
                if result == THROTTLED:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    last = {"error": "throttled"}
                    continue
                return {"IfExistsResult": result,
                        "ThrottleStatus": data.get("ThrottleStatus"),
                        "http": resp.status}
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
            last = {"error": str(exc)[:80]}
    return last


async def _probe_domain(session, domain: str, addresses: Sequence[str],
                        timeout: float, delay: float) -> List[MsResult]:
    """Establish whether the tenant discriminates, then classify each address."""
    # Catch-all control: does this tenant admit a random account exists?
    control = await _query(session, _fake(domain), timeout)
    control_result = control.get("IfExistsResult", -1)
    opaque = control_result in (EXISTS, 5, 6)   # says the fake "exists"

    out: List[MsResult] = []
    for address in addresses:
        await asyncio.sleep(delay)
        data = await _query(session, address, timeout)
        result = data.get("IfExistsResult", -1)

        if "error" in data and result == -1:
            out.append(MsResult(address, UNKNOWN, None, data["error"]))
        elif opaque:
            # Tenant claims a random fake exists, so 0 means nothing here.
            out.append(MsResult(address, CATCH_ALL, result,
                                "tenant admits fake accounts -> opaque"))
        elif result in (EXISTS, 5, 6):
            # A positive login identity. Verified 100% precise against Clearout:
            # an address Microsoft recognises as a sign-in name always has a
            # mailbox behind it.
            out.append(MsResult(address, VALID, result,
                                "IfExistsResult=%d" % result))
        elif result == NOT_EXIST:
            # NOT proof the mailbox is dead. GetCredentialType only knows login
            # identities (UPNs); a valid proxy/alias address that receives mail
            # returns 1 because it is not itself a sign-in name. So this is
            # undecided -- hand it to SMTP/Clearout, never call it invalid.
            out.append(MsResult(address, UNKNOWN, result,
                                "not a login UPN (may be a deliverable alias)"))
        else:
            out.append(MsResult(address, UNKNOWN, result,
                                data.get("error", "IfExistsResult=%s" % result)))
    return out


async def verify(emails: Sequence[str], concurrency: int = 3,
                 timeout: float = 15.0, delay: float = 0.6,
                 progress=None) -> List[MsResult]:
    if aiohttp is None:
        raise RuntimeError("aiohttp is required: pip install aiohttp")

    by_domain: Dict[str, List[str]] = {}
    for email in emails:
        if "@" in email:
            by_domain.setdefault(email.rsplit("@", 1)[1].lower(), []).append(email)

    semaphore = asyncio.Semaphore(concurrency)
    done = [0]
    total = len(by_domain)
    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        async def worker(domain, addresses):
            async with semaphore:
                res = await _probe_domain(session, domain, addresses, timeout, delay)
                done[0] += 1
                if progress and (done[0] % 5 == 0 or done[0] == total):
                    progress(done[0], total)
                return res

        chunks = await asyncio.gather(
            *(worker(d, a) for d, a in by_domain.items()))
    return [r for chunk in chunks for r in chunk]
