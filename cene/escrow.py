"""Two-person (dual-control) escrow for Tier-2 and tainted write actions.

A held action runs only after `approvers_required` *distinct* people approve it. The requesting
agent can never approve its own action; one rejection closes the item. In production, keep items
in Firestore (or Cloud SQL) instead of memory so they survive restarts.
"""
from __future__ import annotations

import uuid


class EscrowError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EscrowService:
    def __init__(self, audit, clock, ttl_seconds: float = 86400):
        self.audit = audit
        self.clock = clock
        self.ttl = ttl_seconds
        self.items: dict[str, dict] = {}
        self.executor = None  # set by the gateway

    def submit(self, claims: dict, tool: str, args: dict, reason: str) -> dict:
        item = {"id": uuid.uuid4().hex[:12], "tool": tool, "args": args, "agent": claims["sub"],
                "requested_by": claims.get("req_by"), "jti": claims["jti"], "claims": claims, "reason": reason,
                "required": int(claims.get("escrow", {}).get("approvers_required", 2)),
                "approvals": [], "status": "pending", "created": self.clock.now()}
        self.items[item["id"]] = item
        self.audit.append({"type": "escrow_submitted", "escrow_id": item["id"], "tool": tool,
                           "agent": item["agent"], "reason": reason})
        return item

    def list_pending(self) -> list[dict]:
        return [{k: v for k, v in i.items() if k != "claims"} for i in self.items.values() if i["status"] == "pending"]

    def _get(self, item_id: str) -> dict:
        item = self.items.get(item_id)
        if item is None:
            raise EscrowError("ESCROW_NOT_FOUND")
        if item["status"] != "pending":
            raise EscrowError("ESCROW_CLOSED")
        if self.clock.now() - item["created"] > self.ttl:
            item["status"] = "expired"
            raise EscrowError("ESCROW_EXPIRED")
        return item

    def approve(self, item_id: str, approver: str) -> dict:
        item = self._get(item_id)
        if approver == item["agent"] or (item.get("requested_by") and approver == item["requested_by"]):
            raise EscrowError("SELF_APPROVAL")
        if approver in item["approvals"]:
            raise EscrowError("DUPLICATE_APPROVER")
        item["approvals"].append(approver)
        self.audit.append({"type": "escrow_approved", "escrow_id": item_id, "approver": approver,
                           "count": len(item["approvals"]), "required": item["required"]})
        if len(item["approvals"]) < item["required"]:
            return {"status": "pending", "approvals": len(item["approvals"]), "required": item["required"]}
        outcome = self.executor(item)
        item["status"] = outcome["status"]
        return outcome

    def reject(self, item_id: str, approver: str, reason: str = "") -> dict:
        item = self._get(item_id)
        item["status"] = "rejected"
        self.audit.append({"type": "escrow_rejected", "escrow_id": item_id, "approver": approver, "reason": reason})
        return {"status": "rejected"}
