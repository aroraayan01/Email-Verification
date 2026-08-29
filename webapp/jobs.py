"""Background job store for bulk verification.

Jobs live in SQLite so a restart doesn't lose them, and results are written to
CSV on disk rather than held in memory -- a 50k-row list should not live in RAM
just to be downloaded once.
"""

import asyncio
import csv
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL,
    stage       TEXT DEFAULT '',
    done        INTEGER DEFAULT 0,
    total       INTEGER DEFAULT 0,
    rows_in     INTEGER DEFAULT 0,
    unique_in   INTEGER DEFAULT 0,
    resolved    INTEGER DEFAULT 0,
    billable    INTEGER DEFAULT 0,
    counts      TEXT DEFAULT '{}',
    error       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT DEFAULT '',
    -- Clearout credits this job actually spent (tier 4).
    credits_spent INTEGER DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, db_path: str, results_dir: str):
        self.db_path = db_path
        self.results_dir = results_dir
        os.makedirs(results_dir, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Migrate job DBs created before tier 4 existed.
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
            if "credits_spent" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN "
                             "credits_spent INTEGER DEFAULT 0")

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, filename: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (id, filename, status, created_at) VALUES (?,?,?,?)",
                (job_id, filename, QUEUED, _now()))
        return job_id

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join("%s = ?" % k for k in fields)
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET %s WHERE id = ?" % sets,
                         tuple(fields.values()) + (job_id,))

    def get(self, job_id: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?",
                               (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["counts"] = json.loads(data.get("counts") or "{}")
        except ValueError:
            data["counts"] = {}
        return data

    def list(self, limit: int = 40) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            try:
                data["counts"] = json.loads(data.get("counts") or "{}")
            except ValueError:
                data["counts"] = {}
            out.append(data)
        return out

    def delete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        paths = [self.result_path(job_id, s) for s in ("all", "clearout", "resolved")]
        paths += [self.enriched_path(job_id, s) for s in ("all", "clearout", "resolved")]
        paths.append(self.source_path(job_id))
        for path in paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        with self._conn() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return True

    # -- results ----------------------------------------------------------

    def result_path(self, job_id: str, which: str = "all") -> str:
        return os.path.join(self.results_dir, "%s_%s.csv" % (job_id, which))

    def source_path(self, job_id: str) -> str:
        return os.path.join(self.results_dir, "%s_source.json" % job_id)

    def enriched_path(self, job_id: str, which: str = "all") -> str:
        return os.path.join(self.results_dir, "%s_%s_full.csv" % (job_id, which))

    # Verdict columns appended after the user's own columns.
    VERDICT_COLS = ["verification_status", "confidence", "checked_by",
                    "mail_host", "reason", "suggested_fix"]

    def write_source(self, job_id, header, rows, email_idx) -> None:
        """Persist the uploaded table verbatim so downloads can rebuild it."""
        with open(self.source_path(job_id), "w", encoding="utf-8") as handle:
            json.dump({"header": header, "rows": rows, "email_idx": email_idx},
                      handle)

    def read_source(self, job_id):
        path = self.source_path(job_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (ValueError, OSError):
            return None

    def _write_enriched(self, job_id: str, verdicts) -> None:
        """Original columns + verdict columns, one file per bucket.

        Rows are matched to verdicts by address, so duplicate addresses each
        get the same verdict and every original row is preserved in 'all'.
        """
        source = self.read_source(job_id)
        if not source:
            return
        rows = source.get("rows") or []
        email_idx = source.get("email_idx") or 0
        header = source.get("header")

        # Map address -> verdict. When an address appears more than once the
        # engine marks later copies "duplicate"; for the enriched file we want
        # every row to carry the address's REAL verdict, so a duplicate marker
        # never overwrites a real one.
        vmap = {}
        for v in verdicts:
            key = (v.email or "").strip().lower()
            if v.status == "duplicate" and key in vmap:
                continue
            vmap[key] = v

        width = max((len(r) for r in rows), default=email_idx + 1)
        if header:
            base_header = header + [""] * (width - len(header))
        else:
            base_header = ["column_%d" % (i + 1) for i in range(width)]
        out_header = base_header + self.VERDICT_COLS

        buckets = {
            "all": None,
            "clearout": "to_vendor",
            "resolved": "siphoned",
        }
        for which, want in buckets.items():
            with open(self.enriched_path(job_id, which), "w", newline="",
                      encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(out_header)
                for row in rows:
                    row = list(row) + [""] * (width - len(row))
                    email = row[email_idx].strip().lower() if email_idx < len(row) else ""
                    v = vmap.get(email)
                    if want is not None and (v is None or v.disposition != want):
                        continue
                    if v is None:
                        writer.writerow(row + [""] * len(self.VERDICT_COLS))
                    else:
                        writer.writerow(row + [
                            v.status,
                            "" if v.confidence is None else v.confidence,
                            v.tier, v.route, v.reason, v.suggestion])

    FIELDS = ["email", "status", "confidence", "disposition", "tier", "route",
              "reason", "suggestion"]

    FIND_FIELDS = ["name", "domain", "email", "status", "confidence", "method"]

    def write_find_results(self, job_id: str, results) -> None:
        with open(self.result_path(job_id, "all"), "w", newline="",
                  encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.FIND_FIELDS)
            for r in results:
                writer.writerow([r.query, r.domain, r.email or "", r.status,
                                 r.confidence, r.method])

    def write_results(self, job_id: str, verdicts) -> None:
        buckets = {
            "all": verdicts,
            "clearout": [v for v in verdicts if v.disposition == "to_vendor"],
            "resolved": [v for v in verdicts if v.disposition == "siphoned"],
        }
        for which, subset in buckets.items():
            with open(self.result_path(job_id, which), "w", newline="",
                      encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(self.FIELDS)
                for v in subset:
                    writer.writerow([
                        v.email, v.status,
                        "" if v.confidence is None else v.confidence,
                        v.disposition, v.tier, v.route, v.reason, v.suggestion])
        # Also write the original-columns-preserved version for downloads.
        self._write_enriched(job_id, verdicts)

    def read_results(self, job_id: str, which: str = "all",
                     status: str = "", limit: int = 500,
                     offset: int = 0) -> Dict:
        path = self.result_path(job_id, which)
        if not os.path.exists(path):
            return {"rows": [], "total": 0}
        rows = []
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if status and row.get("status") != status:
                    continue
                rows.append(row)
        return {"rows": rows[offset:offset + limit], "total": len(rows)}
