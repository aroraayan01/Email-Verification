"""Tier 4: Clearout. The last tier, and the only one that costs money.

Everything above this file exists to avoid reaching it. Local checks, the
Microsoft directory endpoint and SMTP probing settle what they can prove for
free; what survives all of them is genuinely unprovable by us -- catch-all
domains, Google Workspace mailboxes, aliases behind a gateway -- and the only
remaining way to get an answer is to buy one.

Two rules this module exists to enforce:

  * A credit spent is a credit banked. Every answer Clearout gives is written
    to the verdict cache, so the same address is never bought twice.
  * A failed call is not an answer. Network errors, throttling and auth
    failures come back with billed=False and must never be cached -- storing a
    transport failure as 'unknown' would lose the address on this run and burn
    a credit re-learning nothing on the next one.

API: POST {base}/email_verify/instant, bearer token in the Authorization header.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Dict, List, Sequence

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

BASE_URL = "https://api.clearout.io/v2"
INSTANT_PATH = "/email_verify/instant"
CREDITS_PATH = "/email_verify/getcredits"

# Clearout's vocabulary, normalised to ours. Deliberately the same mapping the
# CLI uses when ingesting a Clearout CSV, so a verdict means the same thing
# whether it arrived over the API or in a spreadsheet.
STATUS_MAP = {
    "valid": "valid",
    "invalid": "invalid",
    "catch_all": "catch_all",
    "catchall": "catch_all",
    "accept_all": "catch_all",
    "unknown": "unknown",
    "disposable": "disposable",
    "role": "role",
}


@dataclass
class Config:
    api_key: str = ""
    base_url: str = BASE_URL
    timeout: float = 40.0           # our own HTTP deadline, seconds
    verify_timeout_ms: int = 25000  # how long Clearout may spend probing
    concurrency: int = 8
    max_retries: int = 4

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class VendorResult:
    email: str
    status: str = "unknown"      # our vocabulary
    vendor_status: str = ""      # what Clearout actually said
    safe_to_send: str = ""
    detail: str = ""
    billed: bool = False         # did this consume a credit?
    error: str = ""


class VendorError(RuntimeError):
    """Fatal, whole-run condition: rejected key, or an empty balance."""


def _headers(config: Config) -> Dict[str, str]:
    return {
        "Authorization": "Bearer " + config.api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _interpret(email: str, data: dict) -> VendorResult:
    """Turn one instant-verify payload into a verdict in our vocabulary."""
    body = data.get("data") or {}
    vendor_status = str(body.get("status") or "").strip().lower()
    status = STATUS_MAP.get(vendor_status, "unknown")

    sub = body.get("sub_status") or {}
    desc = ""
    if isinstance(sub, dict):
        desc = str(sub.get("desc") or "").strip()
    elif isinstance(sub, str):
        desc = sub.strip()

    detail = "Clearout: %s" % (vendor_status or "no status")
    if desc:
        detail += " (%s)" % desc

    # Clearout flags these separately from the deliverability verdict. Worth
    # surfacing: the address can be live and still be a bad send.
    flags = [name for name in ("disposable", "role", "gibberish", "free")
             if str(body.get(name) or "").lower() == "yes"]
    if flags:
        detail += " [%s]" % ", ".join(flags)

    return VendorResult(
        email=email,
        status=status,
        vendor_status=vendor_status,
        safe_to_send=str(body.get("safe_to_send") or "").strip().lower(),
        detail=detail,
        billed=True,
    )


async def _verify_one(session, email: str, config: Config,
                      fatal: List[str]) -> VendorResult:
    """One instant-verify call, with backoff on throttling and 5xx.

    `fatal` is a shared one-slot list. The moment any worker hits a condition
    that dooms the whole run -- a rejected key, an empty balance -- it lands
    there and every other worker gives up immediately rather than grinding
    through hundreds of requests that cannot succeed.
    """
    if fatal:
        return VendorResult(email, error=fatal[0])

    url = config.base_url.rstrip("/") + INSTANT_PATH
    payload = json.dumps({"email": email,
                          "timeout": int(config.verify_timeout_ms)}).encode()
    timeout = aiohttp.ClientTimeout(total=config.timeout)
    delay = 2.0
    last = "no attempt"

    for _ in range(max(1, config.max_retries)):
        if fatal:
            return VendorResult(email, error=fatal[0])
        try:
            async with session.post(url, data=payload, headers=_headers(config),
                                    timeout=timeout) as resp:
                text = await resp.text()

                if resp.status in (401, 403):
                    fatal.append("Clearout rejected the API key (HTTP %d)"
                                 % resp.status)
                    return VendorResult(email, error=fatal[0])
                if resp.status == 402:
                    fatal.append("Clearout account is out of credits")
                    return VendorResult(email, error=fatal[0])
                if resp.status in (429, 503, 524) or resp.status >= 500:
                    last = "http %d" % resp.status
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue

                try:
                    data = json.loads(text)
                except ValueError:
                    return VendorResult(email,
                                        error="non-JSON reply from Clearout")

                if str(data.get("status") or "").lower() != "success":
                    # A per-address rejection (malformed input, unsupported
                    # domain). Not retryable, and not billed.
                    return VendorResult(
                        email,
                        error=str(data.get("message") or "rejected")[:120])

                return _interpret(email, data)

        except asyncio.TimeoutError:
            last = "timed out"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:100]
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    return VendorResult(email, error=last)


async def verify(emails: Sequence[str], config: Config,
                 progress=None) -> List[VendorResult]:
    """Verify a batch.

    Never raises for a single bad address -- those come back with an error and
    billed=False. Raises VendorError only when the whole run is doomed, so a
    rejected key surfaces as an error the user can act on instead of a list
    silently marked unknown.
    """
    if aiohttp is None:
        raise VendorError("aiohttp is required: pip install aiohttp")
    if not config.enabled:
        raise VendorError("no Clearout API key configured")

    emails = list(emails)
    if not emails:
        return []

    semaphore = asyncio.Semaphore(max(1, config.concurrency))
    fatal: List[str] = []
    done = [0]
    total = len(emails)
    connector = aiohttp.TCPConnector(limit=max(1, config.concurrency))

    async with aiohttp.ClientSession(connector=connector) as session:
        async def worker(email: str) -> VendorResult:
            async with semaphore:
                result = await _verify_one(session, email, config, fatal)
                done[0] += 1
                if progress and (done[0] % 5 == 0 or done[0] == total):
                    progress(done[0], total)
                return result

        results = await asyncio.gather(*(worker(e) for e in emails))

    if fatal and not any(r.billed for r in results):
        # Nothing came back at all -- surface why, rather than handing back a
        # list of unknowns that looks like a verdict.
        raise VendorError(fatal[0])
    return list(results)


async def credits(config: Config) -> dict:
    """Remaining balance, for the admin console. Never raises."""
    if aiohttp is None or not config.enabled:
        return {"available": None, "error": "not configured"}
    url = config.base_url.rstrip("/") + CREDITS_PATH
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(config),
                                   timeout=timeout) as resp:
                data = json.loads(await resp.text())
    except Exception as exc:  # noqa: BLE001
        return {"available": None, "error": str(exc)[:120]}
    body = data.get("data") or {}
    inner = body.get("credits") or {}
    return {
        "available": body.get("available_credits", inner.get("available")),
        "total": inner.get("total"),
        "daily_remaining": inner.get("available_daily_verify_limit"),
        "error": "" if str(data.get("status") or "").lower() == "success"
                 else str(data.get("message") or "")[:120],
    }
