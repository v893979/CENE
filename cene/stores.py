"""State stores used for single-use request ids, session taint and temporal-policy windows.

MemoryStore - single process (tests, local runs).
RedisStore  - Memorystore for Redis on Google Cloud; shared by all gateway instances.

The gateway fails closed: if the store raises, the call is denied (see gateway.py).
"""
from __future__ import annotations

import json
import threading
import uuid


class MemoryStore:
    def __init__(self):
        self._kv: dict[str, tuple[str, float | None]] = {}
        self._events: dict[str, list[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    # ---- key/value with expiry -------------------------------------------------
    def set_nx(self, key: str, value: str, ttl: float, now: float) -> bool:
        with self._lock:
            cur = self._kv.get(key)
            if cur and (cur[1] is None or cur[1] > now):
                return False
            self._kv[key] = (value, now + ttl if ttl else None)
            return True

    def set(self, key: str, value: str, ttl: float, now: float) -> None:
        with self._lock:
            self._kv[key] = (value, now + ttl if ttl else None)

    def get(self, key: str, now: float) -> str | None:
        with self._lock:
            cur = self._kv.get(key)
            if not cur or (cur[1] is not None and cur[1] <= now):
                return None
            return cur[0]

    # ---- sliding-window events -------------------------------------------------
    def add_event(self, key: str, ts: float, value: float, ttl: float) -> None:
        with self._lock:
            self._events.setdefault(key, []).append((ts, float(value)))
            cutoff = ts - ttl
            self._events[key] = [e for e in self._events[key] if e[0] > cutoff]

    def events_since(self, key: str, since: float) -> list[tuple[float, float]]:
        with self._lock:
            return [e for e in self._events.get(key, []) if e[0] > since]

    def ping(self) -> bool:
        return True


class RedisStore:
    """Redis-backed store. Window events are kept in sorted sets scored by timestamp."""

    def __init__(self, url: str, timeout_s: float | None = None):
        """timeout_s (default CENE_REDIS_TIMEOUT_S, 1.0 s) bounds both connecting and each reply, so an
        unreachable or stalled Redis is refused quickly (STATE_UNAVAILABLE) instead of hanging until the
        OS TCP timeout. Retries are off: a write such as set_nx on a request id must not be repeated, and
        a fail-closed denial is the correct outcome when the store is unavailable."""
        import os

        import redis  # lazy import
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        if timeout_s is None:
            timeout_s = float(os.environ.get("CENE_REDIS_TIMEOUT_S", "1.0"))
        self._r = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=timeout_s,
                                       socket_timeout=timeout_s, retry=Retry(NoBackoff(), 0),
                                       retry_on_timeout=False, retry_on_error=[])

    def set_nx(self, key, value, ttl, now):
        return bool(self._r.set(key, value, nx=True, ex=max(1, int(ttl)) if ttl else None))

    def set(self, key, value, ttl, now):
        self._r.set(key, value, ex=max(1, int(ttl)) if ttl else None)

    def get(self, key, now):
        return self._r.get(key)

    def add_event(self, key, ts, value, ttl):
        member = json.dumps({"id": uuid.uuid4().hex, "v": float(value)})
        pipe = self._r.pipeline()
        pipe.zadd(key, {member: ts})
        pipe.zremrangebyscore(key, "-inf", ts - ttl)
        pipe.expire(key, max(1, int(ttl)))
        pipe.execute()

    def events_since(self, key, since):
        out = []
        for member, score in self._r.zrangebyscore(key, f"({since}", "+inf", withscores=True):
            out.append((score, json.loads(member)["v"]))
        return out

    def ping(self):
        return bool(self._r.ping())


def make_store(redis_url: str | None):
    return RedisStore(redis_url) if redis_url else MemoryStore()
