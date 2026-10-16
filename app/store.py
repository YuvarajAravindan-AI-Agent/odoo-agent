"""SQLite store for proposals (the approval queue) and the append-only audit log.

State machine:  pending -> executing -> executed | failed
                pending -> rejected | expired
The pending -> executing step is a single conditional UPDATE, so two concurrent
approvals of the same proposal cannot both run the write.
"""
import hashlib
import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, session_id TEXT, tool TEXT, args_json TEXT, args_hash TEXT,
  preview_json TEXT, risk TEXT, status TEXT, created_at REAL, expires_at REAL,
  decided_at REAL, decided_by TEXT, result_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, event TEXT,
  proposal_id TEXT, detail_json TEXT
);
"""


def args_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([tool, args], sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.executescript(SCHEMA)

    def _q(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._db.execute(sql, params)

    def audit(self, actor: str, event: str, proposal_id: str | None = None, detail: dict | None = None) -> None:
        self._q(
            "INSERT INTO audit(ts, actor, event, proposal_id, detail_json) VALUES (?,?,?,?,?)",
            (time.time(), actor, event, proposal_id, json.dumps(detail or {}, default=str)),
        )

    def create_proposal(self, session_id: str, tool: str, args: dict, preview: dict, risk: str, ttl: int) -> dict:
        pid = uuid.uuid4().hex[:8]
        now = time.time()
        self._q(
            "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
            (pid, session_id, tool, json.dumps(args), args_hash(tool, args), json.dumps(preview, default=str),
             risk, "pending", now, now + ttl),
        )
        self.audit("agent", "proposed", pid, {"tool": tool, "args": args, "risk": risk})
        return self.get(pid)

    def get(self, pid: str) -> dict | None:
        row = self._q("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._row(row) if row else None

    def list_proposals(self, status: str | None = None, limit: int = 50) -> list[dict]:
        self.expire_stale()
        if status:
            rows = self._q("SELECT * FROM proposals WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self._q("SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows.fetchall()]

    def expire_stale(self) -> None:
        self._q("UPDATE proposals SET status='expired' WHERE status='pending' AND expires_at<?", (time.time(),))

    def claim(self, pid: str, who: str) -> bool:
        """Atomically move pending -> executing. False if already decided or expired."""
        cur = self._q(
            "UPDATE proposals SET status='executing', decided_at=?, decided_by=? "
            "WHERE id=? AND status='pending' AND expires_at>=?",
            (time.time(), who, pid, time.time()),
        )
        return cur.rowcount == 1

    def reject(self, pid: str, who: str) -> bool:
        cur = self._q(
            "UPDATE proposals SET status='rejected', decided_at=?, decided_by=? WHERE id=? AND status='pending'",
            (time.time(), who, pid),
        )
        if cur.rowcount == 1:
            self.audit(who, "rejected", pid)
        return cur.rowcount == 1

    def finish(self, pid: str, result: dict | None, error: str | None) -> None:
        self._q(
            "UPDATE proposals SET status=?, result_json=?, error=? WHERE id=?",
            ("failed" if error else "executed", json.dumps(result, default=str) if result is not None else None, error, pid),
        )

    def audit_log(self, limit: int = 100) -> list[dict]:
        rows = self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("args_json", "preview_json", "result_json"):
            d[k[:-5]] = json.loads(d.pop(k)) if d[k] else None
        return d
