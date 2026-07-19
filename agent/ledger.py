"""
Append-only, hash-chained audit ledger.

Every action the agent takes is written here BEFORE side effects complete.
Each row's hash includes the previous row's hash, so any after-the-fact
tampering breaks the chain and is detectable with verify_chain().

This is the piece that makes claims about your agent provable.
"""
import hashlib
import json
import threading
from datetime import datetime, timezone
from agent.db import db

GENESIS = "0" * 64
_chain_lock = threading.Lock()  # scheduler + collector threads both append;
                                # read-prev-hash + insert must be atomic or the
                                # chain forks. In-process lock + WAL is sufficient
                                # for this single-process design.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_hash(ts: str, actor: str, action: str, detail: str, cost: float, prev: str) -> str:
    payload = f"{ts}|{actor}|{action}|{detail}|{cost:.4f}|{prev}"
    return hashlib.sha256(payload.encode()).hexdigest()


def record(actor: str, action: str, detail: dict, cost_usd: float = 0.0) -> int:
    """Append an entry. Returns row id."""
    detail_json = json.dumps(detail, sort_keys=True, default=str)
    ts = _now()
    with _chain_lock, db() as conn:
        row = conn.execute("SELECT hash FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        h = _row_hash(ts, actor, action, detail_json, cost_usd, prev)
        cur = conn.execute(
            "INSERT INTO ledger (ts, actor, action, detail, cost_usd, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, actor, action, detail_json, cost_usd, prev, h),
        )
        return cur.lastrowid


def verify_chain() -> dict:
    """Recompute every hash. Returns {'ok': bool, 'entries': n, 'first_bad_id': id|None}."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM ledger ORDER BY id").fetchall()
    prev = GENESIS
    for r in rows:
        expected = _row_hash(r["ts"], r["actor"], r["action"], r["detail"], r["cost_usd"], prev)
        if expected != r["hash"] or r["prev_hash"] != prev:
            return {"ok": False, "entries": len(rows), "first_bad_id": r["id"]}
        prev = r["hash"]
    return {"ok": True, "entries": len(rows), "first_bad_id": None}


def total_spend(since_iso: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(cost_usd),0) AS s FROM ledger WHERE action='purchase.execute'"
    args: tuple = ()
    if since_iso:
        q += " AND ts >= ?"
        args = (since_iso,)
    with db() as conn:
        return float(conn.execute(q, args).fetchone()["s"])


def recent(limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
