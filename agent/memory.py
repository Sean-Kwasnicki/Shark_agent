"""
Persistent long-term memory. Simple, deterministic keyword + importance +
recency retrieval — no embeddings dependency at v1. Swap retrieve() for a
vector search later without touching callers.
"""
from datetime import datetime, timezone
from agent.db import db
from agent import ledger


def write(kind: str, content: str, tags: str = "", importance: int = 3) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    importance = max(1, min(5, int(importance)))
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO memory (ts, kind, content, tags, importance) VALUES (?,?,?,?,?)",
            (ts, kind, content, tags, importance),
        )
        mem_id = cur.lastrowid
    ledger.record("agent", "memory.write", {"id": mem_id, "kind": kind, "tags": tags})
    return mem_id


def retrieve(query: str = "", limit: int = 12) -> list[dict]:
    """Rank by (keyword overlap * importance), tie-break on recency."""
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT 500")]
    if not query:
        rows.sort(key=lambda r: (r["importance"], r["ts"]), reverse=True)
        return rows[:limit]
    terms = {t for t in query.lower().split() if len(t) > 2}
    def score(r):
        text = (r["content"] + " " + r["tags"]).lower()
        overlap = sum(1 for t in terms if t in text)
        return (overlap * r["importance"], r["ts"])
    rows.sort(key=score, reverse=True)
    return rows[:limit]


def context_block(query: str = "") -> str:
    """Format retrieved memories for injection into the planner prompt."""
    mems = retrieve(query)
    if not mems:
        return "No stored memories yet."
    return "\n".join(f"- [{m['kind']}|imp{m['importance']}] {m['content']}" for m in mems)
