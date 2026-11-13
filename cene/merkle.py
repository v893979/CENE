"""Merkle commitments for token bindings.

Large binding sets are not embedded in the token. The token carries only the Merkle root (signed),
and the control plane returns each binding with its inclusion proof. On a call the agent attaches
the proof for the binding it is using; the gateway recomputes the root. A binding the control plane
never issued has no valid proof, so it is refused exactly as if it were missing from an inline list.

leaf  = SHA256(0x00 || canonical_json(binding))
node  = SHA256(0x01 || left || right)        (odd node at a level is promoted unchanged)
proof = [[sibling_hex, "L" | "R"], ...] from leaf to root
"""
from __future__ import annotations

import hashlib

from .util import canonical_json


def leaf_hash(binding: dict) -> bytes:
    return hashlib.sha256(b"\x00" + canonical_json(binding)).digest()


def _node(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + a + b).digest()


def build(bindings: list[dict]) -> tuple[str, list[list]]:
    """Return (root_hex, proofs) where proofs[i] proves bindings[i]."""
    if not bindings:
        return hashlib.sha256(b"\x02empty").hexdigest(), []
    level = [leaf_hash(b) for b in bindings]
    proofs: list[list] = [[] for _ in bindings]
    pos = list(range(len(bindings)))  # position of each leaf's ancestor in the current level
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i])
        for leaf, p in enumerate(pos):
            sib = p ^ 1
            if sib < len(level):
                proofs[leaf].append([level[sib].hex(), "L" if sib < p else "R"])
            pos[leaf] = p // 2
        level = nxt
    return level[0].hex(), proofs


def verify(binding: dict, proof: list, root_hex: str) -> bool:
    try:
        h = leaf_hash(binding)
        for sib_hex, side in proof:
            sib = bytes.fromhex(sib_hex)
            h = _node(sib, h) if side == "L" else _node(h, sib)
        return h.hex() == root_hex
    except (ValueError, TypeError):
        return False
