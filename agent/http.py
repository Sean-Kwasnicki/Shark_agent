"""
HTTP reliability layer: exponential backoff on 429/5xx and an idempotency
store so retried operations (posts, mints, links) never execute twice.
"""
import time
import json
import requests
from datetime import datetime, timezone
from agent.db import db
from agent import ledger

IDEMPOTENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


def init():
    with db() as conn:
        conn.executescript(IDEMPOTENCY_SCHEMA)


def seen(key: str):
    """Return cached result dict if this operation already ran, else None."""
    with db() as conn:
        row = conn.execute("SELECT result FROM idempotency WHERE key=?", (key,)).fetchone()
    return json.loads(row["result"]) if row else None


def remember(key: str, result: dict):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO idempotency (key, ts, result) VALUES (?,?,?)",
            (key, datetime.now(timezone.utc).isoformat(), json.dumps(result, default=str)),
        )


def request_with_retry(method: str, url: str, *, max_tries: int = 4,
                       base_delay: float = 2.0, **kwargs) -> requests.Response:
    """Retry on 429 and 5xx with exponential backoff (2s, 4s, 8s). Raises on final failure."""
    kwargs.setdefault("timeout", 30)
    last_exc = None
    for attempt in range(max_tries):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                ledger.record("system", "http.retry",
                              {"url": url, "status": resp.status_code, "attempt": attempt + 1})
                time.sleep(min(base_delay * (2 ** attempt), 3600))
                continue
            return resp
        except requests.RequestException as e:
            last_exc = e
            ledger.record("system", "http.error", {"url": url, "err": str(e), "attempt": attempt + 1})
            time.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise requests.RequestException(f"Exhausted retries for {url}")
