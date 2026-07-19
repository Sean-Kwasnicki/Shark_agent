"""
Research tools. web_search uses Brave Search API (set BRAVE_API_KEY) —
swap for any provider. fetch_page pulls raw text from a URL after the
constraint engine clears it.
"""
import os
import requests
from agent import ledger, constraints

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")


def web_search(query: str, count: int = 5) -> list[dict]:
    if not BRAVE_API_KEY:
        ledger.record("system", "research.web_search",
                      {"query": query, "error": "BRAVE_API_KEY not set"})
        return []
    r = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": BRAVE_API_KEY},
        params={"q": query, "count": count},
        timeout=20,
    )
    r.raise_for_status()
    results = [
        {"title": i.get("title", ""), "url": i.get("url", ""),
         "snippet": i.get("description", "")}
        for i in r.json().get("web", {}).get("results", [])[:count]
    ]
    ledger.record("agent", "research.web_search", {"query": query, "n": len(results)})
    return results


def fetch_page(url: str, max_chars: int = 8000) -> str:
    constraints.check_url_allowed(url)
    r = requests.get(url, timeout=20, headers={"User-Agent": "loop-agent/0.1"})
    r.raise_for_status()
    text = r.text[:max_chars]
    ledger.record("agent", "research.fetch_page", {"url": url, "chars": len(text)})
    return text
