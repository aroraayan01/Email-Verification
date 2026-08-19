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
    finished_at TEXT DEFAULT ''
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
        for suffix in ("all", "clearout", "resolved"):
            path = self.result_path(job_id, suffix)
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
