"""SQLite-backed verdict cache.

Every address Clearout has ever charged you for is a paid asset. This keeps
them so you never buy the same verdict twice, and so past result exports can
answer new lists for free.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    canonical    TEXT PRIMARY KEY,
    last_seen_as TEXT NOT NULL,
    source       TEXT NOT NULL,
    status       TEXT NOT NULL,
    sub_status   TEXT,
    checked_at   TEXT NOT NULL,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_status  ON verdicts(status);
CREATE INDEX IF NOT EXISTS idx_verdicts_checked ON verdicts(checked_at);

CREATE TABLE IF NOT EXISTS domains (
    domain        TEXT PRIMARY KEY,
    mail_capable  INTEGER,
    mx_hosts      TEXT,
    gateway       TEXT,
    provider      TEXT,
    dns_status    TEXT NOT NULL,
    checked_at    TEXT NOT NULL
);

-- Disagreements between our verdict and Clearout's. This table is the whole
-- point of shadow mode: it is the evidence for or against ever promoting the
-- local pipeline to authoritative.
CREATE TABLE IF NOT EXISTS shadow_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical     TEXT NOT NULL,
    domain        TEXT,
    gateway       TEXT,
    provider      TEXT,
    local_status  TEXT NOT NULL,
    vendor_status TEXT NOT NULL,
    agreed        INTEGER NOT NULL,
    logged_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_agreed ON shadow_log(agreed);

-- Learned address-format shapes per domain, accumulated across every list
-- ever run. This is what makes pattern scoring improve with use: a shape
-- proven valid on one list informs the next.
CREATE TABLE IF NOT EXISTS domain_shapes (
    domain     TEXT NOT NULL,
    shape      TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, shape)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Cache:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- verdicts ---------------------------------------------------------

    def lookup(self, canonical: str, ttl_days: int) -> Optional[sqlite3.Row]:
        """Return a cached verdict if one exists and is still inside its TTL.

        'unknown' is never served from cache -- an undecided address must be
        re-decided, or the cache would silently make the gap permanent.
        """
        row = self.conn.execute(
            "SELECT * FROM verdicts WHERE canonical = ?", (canonical,)
        ).fetchone()
        if row is None or row["status"] == "unknown":
            return None
        try:
            checked = datetime.fromisoformat(row["checked_at"])
        except ValueError:
            return None
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - checked > timedelta(days=ttl_days):
            return None
        return row

    def put(self, canonical: str, seen_as: str, source: str, status: str,
            sub_status: str = "", raw: Optional[dict] = None,
            checked_at: Optional[str] = None) -> None:
        self.conn.execute(
            """INSERT INTO verdicts
                   (canonical, last_seen_as, source, status, sub_status,
                    checked_at, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(canonical) DO UPDATE SET
                   last_seen_as = excluded.last_seen_as,
                   source       = excluded.source,
                   status       = excluded.status,
                   sub_status   = excluded.sub_status,
                   checked_at   = excluded.checked_at,
                   raw          = excluded.raw""",
            (canonical, seen_as, source, status, sub_status,
             checked_at or _now(), json.dumps(raw) if raw else None),
        )

    # -- domains ----------------------------------------------------------

    def get_domain(self, domain: str, ttl_days: int) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT * FROM domains WHERE domain = ?", (domain,)
        ).fetchone()
        if row is None or row["dns_status"] == "error":
            return None
        try:
            checked = datetime.fromisoformat(row["checked_at"])
        except ValueError:
            return None
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - checked > timedelta(days=ttl_days):
            return None
        return row

    def put_domain(self, domain: str, mail_capable: Optional[bool],
                   mx_hosts: Iterable[str], gateway: str, provider: str,
                   dns_status: str) -> None:
        self.conn.execute(
            """INSERT INTO domains
                   (domain, mail_capable, mx_hosts, gateway, provider,
                    dns_status, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET
                   mail_capable = excluded.mail_capable,
                   mx_hosts     = excluded.mx_hosts,
                   gateway      = excluded.gateway,
                   provider     = excluded.provider,
                   dns_status   = excluded.dns_status,
                   checked_at   = excluded.checked_at""",
            (domain,
             None if mail_capable is None else int(mail_capable),
             ",".join(mx_hosts), gateway, provider, dns_status, _now()),
        )

    # -- shadow log -------------------------------------------------------

    def log_shadow(self, canonical: str, domain: str, gateway: str,
                   provider: str, local_status: str,
                   vendor_status: str) -> None:
        self.conn.execute(
            """INSERT INTO shadow_log
                   (canonical, domain, gateway, provider, local_status,
                    vendor_status, agreed, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (canonical, domain, gateway, provider, local_status, vendor_status,
             int(local_status == vendor_status), _now()),
        )

    def shadow_summary(self) -> Dict[str, int]:
        """Counts that matter: agreement, and the two error directions."""
        rows = self.conn.execute(
            """SELECT local_status, vendor_status, COUNT(*) AS n
                 FROM shadow_log GROUP BY local_status, vendor_status"""
        ).fetchall()
        out = {"total": 0, "agreed": 0,
               "false_positive": 0, "false_negative": 0}
        for row in rows:
            out["total"] += row["n"]
            if row["local_status"] == row["vendor_status"]:
                out["agreed"] += row["n"]
            elif row["local_status"] == "valid" and row["vendor_status"] == "invalid":
                # We would have mailed a dead address. The costly direction.
                out["false_positive"] += row["n"]
            elif row["local_status"] == "invalid" and row["vendor_status"] == "valid":
                # We would have discarded a real lead. Cheap but wasteful.
                out["false_negative"] += row["n"]
        return out

    def counts(self) -> Tuple[int, int]:
        verdicts = self.conn.execute(
            "SELECT COUNT(*) FROM verdicts").fetchone()[0]
        domains = self.conn.execute(
            "SELECT COUNT(*) FROM domains").fetchone()[0]
        return verdicts, domains

    def status_breakdown(self) -> Dict[str, int]:
        return {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM verdicts GROUP BY status")
        }

    # -- learned domain formats -------------------------------------------

    def learn_shape(self, domain: str, shape: str) -> None:
        self.conn.execute(
            """INSERT INTO domain_shapes (domain, shape, count, updated_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(domain, shape) DO UPDATE SET
                   count = count + 1, updated_at = excluded.updated_at""",
            (domain, shape, _now()))

    def load_shapes(self, domains) -> Dict[str, Dict[str, int]]:
        """Return {domain: {shape: count}} for the domains asked for."""
        out: Dict[str, Dict[str, int]] = {}
        domains = list(domains)
        if not domains:
            return out
        marks = ",".join("?" * len(domains))
        rows = self.conn.execute(
            "SELECT domain, shape, count FROM domain_shapes "
            "WHERE domain IN (%s)" % marks, domains).fetchall()
        for row in rows:
            out.setdefault(row["domain"], {})[row["shape"]] = row["count"]
        return out

    def commit(self) -> None:
        self.conn.commit()
