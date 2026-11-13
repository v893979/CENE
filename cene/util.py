"""Small shared helpers: clock, canonical JSON, base64url, hashing."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any


class Clock:
    """Wall clock with an adjustable offset so tests and scenarios can move time forward."""

    def __init__(self, start: float | None = None):
        self._offset = 0.0 if start is None else start - time.time()

    def now(self) -> float:
        return time.time() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding used for signing and hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
