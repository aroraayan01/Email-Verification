"""Static reference data used by the pre-filter.

Everything here is deliberately conservative. A domain landing in one of these
sets changes how an address is *routed*, and only NXDOMAIN-style hard evidence
is ever allowed to mark something invalid.
"""

# Small, high-confidence builtin list. This is a floor, not a substitute for a
# maintained list -- point --disposable-file at the disposable-email-domains
# project dump for real coverage.
DISPOSABLE_DOMAINS = {
    "10minutemail.com", "guerrillamail.com", "mailinator.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "getnada.com", "sharklasers.com", "maildrop.cc", "dispostable.com",
    "fakeinbox.com", "mytemp.email", "tempmailo.com", "mohmal.com",
    "emailondeck.com", "spamgourmet.com", "burnermail.io", "temp-mail.io",
}

# Role accounts are NOT dropped -- for B2B outreach info@/sales@ are often the
# intended target. They are flagged so you can decide per campaign.
ROLE_LOCALS = {
    "admin", "administrator", "info", "support", "sales", "contact", "help",
    "billing", "office", "hello", "enquiries", "enquiry", "inquiries",
    "marketing", "team", "hr", "jobs", "careers", "press", "media", "legal",
    "accounts", "accounting", "finance", "noreply", "no-reply", "donotreply",
    "postmaster", "webmaster", "abuse", "security", "privacy", "compliance",
}

# MX hostname fragment -> gateway name.
#
# A domain fronted by one of these answers for the security appliance, not the
# mailbox. RCPT TO against them is unreliable in both directions, so they are
# routed to the paid tier regardless of what a probe would say.
GATEWAY_PATTERNS = [
    ("pphosted.com", "Proofpoint"),
    ("ppe-hosted.com", "Proofpoint"),
    ("mimecast.com", "Mimecast"),
    ("mimecast.co.za", "Mimecast"),
    ("barracudanetworks.com", "Barracuda"),
    ("barracuda.com", "Barracuda"),
    ("iphmx.com", "Cisco IronPort"),
    ("cisco.com", "Cisco"),
    ("sophos.com", "Sophos"),
    ("mailcontrol.com", "Forcepoint"),
    ("trendmicro.com", "Trend Micro"),
    ("emailsrvr.com", "Rackspace"),
    ("messagelabs.com", "Symantec"),
    ("securence.com", "Securence"),
    ("spamtitan.com", "SpamTitan"),
    ("hornetsecurity.com", "Hornetsecurity"),
    ("antispamcloud.com", "SpamExperts"),
    ("mailanyone.net", "Fusemail"),
]

# Large providers that behave as catch-alls under probing. SMTP verification
# against these is close to worthless, so they stay on the paid tier too.
OPAQUE_PROVIDERS = [
    ("google.com", "Google"),
    ("googlemail.com", "Google"),
    ("outlook.com", "Microsoft"),
    ("protection.outlook.com", "Microsoft"),
    ("hotmail.com", "Microsoft"),
    ("yahoodns.net", "Yahoo"),
    ("icloud.com", "Apple"),
    ("me.com", "Apple"),
    ("zoho.com", "Zoho"),
    ("zohomail.com", "Zoho"),
    ("yandex.net", "Yandex"),
    ("qq.com", "Tencent"),
    ("163.com", "NetEase"),
]

# Providers where local-part normalisation rules are known and safe to apply.
DOT_INSENSITIVE = {"gmail.com", "googlemail.com"}

PLUS_ADDRESSING = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "fastmail.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "zoho.com",
}

GOOGLEMAIL_ALIASES = {"googlemail.com": "gmail.com"}

# Used only for typo *suggestions*. Nothing is ever auto-corrected.
COMMON_DOMAINS = [
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com",
    "proton.me", "zoho.com", "fastmail.com", "gmx.com", "gmx.net",
    "mail.com", "yandex.com", "rediffmail.com", "comcast.net", "verizon.net",
    "att.net", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net",
]
