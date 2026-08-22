"""Rate limiting - the doc calls for a Redis-backed token bucket so limits
survive across multiple app instances; this is a single-process app, so an
in-memory bucket gives the exact same protection (bound a client to N
requests per window, return 429 with Retry-After past that) without a new
dependency. Swapping the backing store for Redis later is a constructor
change, not a rewrite - every call site already goes through check(), not
a dict directly.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_buckets: dict[str, list[float]] = {}  # key -> timestamps of recent hits, pruned as we go


def _prune(hits: list[float], window_s: float, now: float) -> list[float]:
    cutoff = now - window_s
    return [t for t in hits if t > cutoff]


def check(key: str, limit: int, window_s: float) -> tuple[bool, int]:
    """True if this call is allowed under `limit` hits per `window_s`
    seconds for `key` (e.g. "ip:1.2.3.4" or "login:someone@example.com").
    Returns (allowed, retry_after_seconds) - retry_after is 0 when allowed."""
    now = time.time()
    with _lock:
        hits = _prune(_buckets.get(key, []), window_s, now)
        if len(hits) >= limit:
            retry_after = int(window_s - (now - hits[0])) + 1
            _buckets[key] = hits  # keep pruned, don't record the rejected attempt itself
            return False, max(1, retry_after)
        hits.append(now)
        _buckets[key] = hits
        return True, 0


def client_ip(request) -> str:
    """Best-effort real client IP - trusts X-Forwarded-For only because
    this demo has no reverse proxy in front of it stripping/setting that
    header itself; a real deployment behind one needs to trust only the
    proxy's own hop, not blindly the whole header."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
