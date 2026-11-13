"""Tamper-evident audit log.

Every decision is appended as an event. Events are hash-chained:
    h_0 = SHA256("CENE-AUDIT-GENESIS")
    h_i = SHA256(h_{i-1} || canonical_json(event_i))
A checkpoint signs (seq, head hash, time) with the audit signing key (Cloud KMS/HSM in production).
Changing, inserting or deleting any earlier event changes every later hash, so verification fails.

Sinks: LocalFileSink (JSON lines) or GcsSink (Cloud Storage bucket with a retention policy /
bucket lock, i.e. write-once storage). Run the audit writer as a single instance so the chain is
linear (Cloud Run max-instances = 1), or keep one chain per instance and checkpoint each.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from .signer import verify_es256
from .util import b64url, b64url_decode, canonical_json

GENESIS = hashlib.sha256(b"CENE-AUDIT-GENESIS").hexdigest()


def chain_hash(prev: str, event: dict) -> str:
    return hashlib.sha256(prev.encode() + canonical_json(event)).hexdigest()


class MemorySink:
    def __init__(self):
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


class LocalFileSink:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")


class GcsSink:
    """One object per record: gs://bucket/audit/<service>/<chain id>/<seq>-<kind>.json.

    Each running instance keeps its own chain (control and gateway both write, and Cloud Run
    restarts instances), so every chain gets its own folder: the service name plus the instance's
    start time and a random suffix. Objects are created with if_generation_match=0, so an existing
    record can never be overwritten; the writer needs only roles/storage.objectCreator.
    Pair with a bucket retention lock.
    """

    def __init__(self, bucket: str, prefix: str = "audit", chain_id: str | None = None):
        import os
        import time
        import uuid

        from google.cloud import storage  # lazy import

        self._bucket = storage.Client().bucket(bucket)
        service = os.environ.get("K_SERVICE") or os.environ.get("CENE_SERVICE", "cene")
        self.chain_id = chain_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        self._prefix = f"{prefix}/{service}/{self.chain_id}"

    def write(self, record: dict) -> None:
        name = f"{self._prefix}/{record['seq']:012d}-{record['kind']}.json"
        self._bucket.blob(name).upload_from_string(json.dumps(record, sort_keys=True),
                                                   content_type="application/json",
                                                   if_generation_match=0)


class AuditLog:
    def __init__(self, sink, signer, clock, checkpoint_every: int = 100):
        self.sink = sink
        self.signer = signer
        self.clock = clock
        self.every = checkpoint_every
        self.head = GENESIS
        self.seq = 0
        self._signed_seq = 0
        self._lock = threading.Lock()

    def append(self, event: dict) -> dict:
        with self._lock:
            ev = {"ts": round(self.clock.now(), 3), **event}
            self.seq += 1
            self.head = chain_hash(self.head, ev)
            rec = {"kind": "event", "seq": self.seq, "event": ev, "hash": self.head}
            self.sink.write(rec)
            if self.seq % self.every == 0:
                self._checkpoint_locked()
            return rec

    def checkpoint(self) -> dict:
        with self._lock:
            return self._checkpoint_locked()

    def close(self) -> dict | None:
        """Sign the tail of the chain. Call on shutdown: a Cloud Run instance usually stops long before
        `checkpoint_every` events, and an unsigned tail could be truncated without detection."""
        with self._lock:
            if self.seq == 0 or self._signed_seq == self.seq:
                return None
            return self._checkpoint_locked()

    def _checkpoint_locked(self) -> dict:
        body = {"seq": self.seq, "head": self.head, "ts": round(self.clock.now(), 3), "kid": self.signer.key_id}
        sig = self.signer.sign(canonical_json(body))
        rec = {"kind": "checkpoint", "seq": self.seq, "body": body, "sig": b64url(sig)}
        self.sink.write(rec)
        self._signed_seq = self.seq
        return rec


def verify_records(records: list[dict], public_pem: bytes) -> tuple[bool, str]:
    """Recompute the chain and check every checkpoint signature."""
    head, seq = GENESIS, 0
    for r in records:
        if r["kind"] == "event":
            seq += 1
            if r["seq"] != seq:
                return False, f"sequence gap at {seq}"
            head = chain_hash(head, r["event"])
            if head != r["hash"]:
                return False, f"hash mismatch at seq {seq}"
        elif r["kind"] == "checkpoint":
            b = r["body"]
            if b["seq"] != seq or b["head"] != head:
                return False, f"checkpoint {b['seq']} does not match chain"
            if not verify_es256(public_pem, canonical_json(b), b64url_decode(r["sig"])):
                return False, f"bad checkpoint signature at {b['seq']}"
    return True, f"ok ({seq} events)"
