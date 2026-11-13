"""Enforcement gateway: every agent tool call passes through here (MCP / JSON-RPC 'tools/call').

Checks, in order (first failure denies the call and closes the connection):
  1  token signature, type, audience, expiry               TOKEN_*
  2  request id not seen before for this token              REPLAY
  3  tool listed in the token                               TOOL_NOT_ALLOWED
  4  arguments: no missing, no extra, each within bounds    ARG_MISSING / ARG_UNEXPECTED / ARG_OUT_OF_BOUNDS
  5  argument combination matches a bound row               BINDING_VIOLATION (or escrow if the tool
                                                            says on_unbound: escrow)
  6  temporal rules (once_per, sum_within, count_within)    TEMPORAL_VIOLATION
  7  routing: escrow tools, or tainted session -> escrow    (decision = escrow)
Then the tool runs, temporal events are recorded, and the session is tainted if the tool is marked
untrusted or a content screener (e.g. Model Armor) flags the result. The decision is written to the
audit log. Any error in the state store denies the call (fail closed).
"""
from __future__ import annotations

import json
import re

from . import merkle
from .control_plane import AUDIENCE
from .tokens import TokenError, verify
from .util import sha256_hex, canonical_json


class Denied(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _check_arg(name: str, spec: dict, value) -> None:
    if value is None and spec.get("nullable"):  # optional argument left unset
        return
    if isinstance(value, (dict, list)) or value is None:
        raise Denied("ARG_OUT_OF_BOUNDS", f"{name}: must be a scalar")
    if "pattern" in spec:
        if not isinstance(value, str) or not re.fullmatch(spec["pattern"], value):
            raise Denied("ARG_OUT_OF_BOUNDS", f"{name}: does not match pattern")
    if spec.get("type") == "number" or "max" in spec or "min" in spec:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Denied("ARG_OUT_OF_BOUNDS", f"{name}: must be a number")
        if "min" in spec and value < float(spec["min"]):
            raise Denied("ARG_OUT_OF_BOUNDS", f"{name}: below minimum")
        if "max" in spec and value > float(spec["max"]):
            raise Denied("ARG_OUT_OF_BOUNDS", f"{name}: above maximum")
    if "enum" in spec and value not in spec["enum"]:
        raise Denied("ARG_OUT_OF_BOUNDS", f"{name}: not an allowed value")


def _within(value, m: float) -> bool:
    if value is None:  # a nullable argument left unset changes nothing, so no ceiling applies
        return True
    return float(value) <= m


def _binding_ok(bindings: list[dict], args: dict) -> bool:
    for b in bindings:
        if all(args.get(k) == v for k, v in b["k"].items()) and \
           all(_within(args.get(a, float("inf")), m) for a, m in b.get("max", {}).items()):
            return True
    return False


class Gateway:
    def __init__(self, keyring, state, tools, escrow, audit, clock, screener=None):
        from .screening import NoScreener

        self.screener = screener or NoScreener()
        self.keyring = keyring
        self.state = state  # MemoryStore / RedisStore
        self.tools = tools
        self.escrow = escrow
        self.audit = audit
        self.clock = clock
        escrow.executor = self.execute_approved

    # ------------------------------------------------------------------ temporal rules
    def _rule_key(self, rule: dict, claims: dict, args: dict) -> str:
        parts = [claims["sub"] if b == "agent" else args.get(b) for b in rule.get("by", [])]
        return "tmp:" + rule["name"] + ":" + sha256_hex(canonical_json(parts))[:24]

    def _check_temporal(self, claims: dict, tool: str, args: dict) -> None:
        now = self.clock.now()
        for r in claims.get("temporal", []):
            if r["tool"] != tool:
                continue
            events = self.state.events_since(self._rule_key(r, claims, args), now - float(r["window_seconds"]))
            kind = r["type"]
            if kind == "once_per" and events:
                raise Denied("TEMPORAL_VIOLATION", r["name"])
            if kind == "count_within" and len(events) + 1 > int(r["max"]):
                raise Denied("TEMPORAL_VIOLATION", r["name"])
            if kind == "sum_within" and sum(v for _, v in events) + float(args.get(r["field"], 0)) > float(r["max"]):
                raise Denied("TEMPORAL_VIOLATION", r["name"])

    def _commit_temporal(self, claims: dict, tool: str, args: dict) -> None:
        now = self.clock.now()
        for r in claims.get("temporal", []):
            if r["tool"] == tool:
                val = float(args.get(r["field"], 0)) if r["type"] == "sum_within" else 1.0
                self.state.add_event(self._rule_key(r, claims, args), now, val, float(r["window_seconds"]))

    # ------------------------------------------------------------------ main entry
    def handle(self, token: str | None, request: dict) -> tuple[dict, dict]:
        """Returns (json_rpc_response, decision). decision['close'] tells the server to drop the connection."""
        rid = request.get("id")
        params = request.get("params") or {}
        tool, args = params.get("name"), params.get("arguments") or {}
        claims = None
        try:
            if request.get("method") != "tools/call":
                raise Denied("METHOD_NOT_ALLOWED")
            if not token:
                raise Denied("TOKEN_MISSING")
            try:
                claims = verify(token, self.keyring, self.clock.now(), AUDIENCE)
            except TokenError as e:
                raise Denied(e.code) from e
            if rid is None:
                raise Denied("REQUEST_ID_MISSING")
            try:
                fresh_id = self.state.set_nx(f"rid:{claims['jti']}:{rid}", "1",
                                             max(60.0, claims["exp"] - self.clock.now() + 60), self.clock.now())
            except Exception as e:
                raise Denied("STATE_UNAVAILABLE", str(e)) from e
            if not fresh_id:
                raise Denied("REPLAY")
            spec = claims["tools"].get(tool)
            if spec is None:
                raise Denied("TOOL_NOT_ALLOWED", str(tool))
            for a in spec["args"]:
                if a not in args:
                    raise Denied("ARG_MISSING", a)
            for a in args:
                if a not in spec["args"]:
                    raise Denied("ARG_UNEXPECTED", a)
            for a, s in spec["args"].items():
                _check_arg(a, s, args[a])
            # A call that matches no binding is denied, unless the tool routes unbound calls to escrow.
            escrow_unbound = spec.get("on_unbound") == "escrow"
            unbound = False
            if "bindings" in spec and not _binding_ok(spec["bindings"], args):
                if not escrow_unbound:
                    raise Denied("BINDING_VIOLATION")
                unbound = True
            if "bindings_root" in spec:
                proof = (params.get("_meta") or {}).get("binding_proof") or {}
                try:
                    ok = merkle.verify(proof.get("binding"), proof.get("proof"), spec["bindings_root"]) \
                        and _binding_ok([proof["binding"]], args)
                except (KeyError, TypeError, AttributeError, ValueError):
                    ok = False
                if not ok:
                    if not escrow_unbound:
                        raise Denied("BINDING_VIOLATION", "no valid binding proof")
                    unbound = True
            try:
                self._check_temporal(claims, tool, args)
                tainted = self.state.get(f"taint:{claims['jti']}", self.clock.now()) is not None
            except Denied:
                raise
            except Exception as e:
                raise Denied("STATE_UNAVAILABLE", str(e)) from e

            route_escrow = spec["mode"] == "escrow" or unbound
            if tainted and spec["kind"] == "write":
                if claims["taint"]["on_taint"] == "deny":
                    raise Denied("TAINTED_SESSION")
                route_escrow = True
            if route_escrow:
                reason = "tainted_session" if tainted else "unbound" if unbound else "tier2"
                item = self.escrow.submit(claims, tool, args, reason=reason)
                return self._ok(rid, {"status": "pending_approval", "escrow_id": item["id"]},
                                claims, tool, args, "escrow", item["id"])

            try:
                result = self.tools.call(tool, args, claims)
            except Exception as e:  # the tool itself failed: nothing executed, nothing recorded
                self.audit.append({"type": "tool_error", "jti": claims["jti"], "agent": claims["sub"], "tool": tool,
                                   "error": type(e).__name__})
                return ({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "TOOL_ERROR",
                                                                 "data": type(e).__name__}},
                        {"decision": "error", "code": "TOOL_ERROR", "close": False})
            self._commit_temporal(claims, tool, args)
            taint_reason = "untrusted_tool" if (tool in claims["taint"]["untrusted_tools"]
                                                 or result.get("untrusted")) else None
            if spec["kind"] == "read" and not taint_reason:
                finding = self.screener.screen(json.dumps(result, default=str))
                if finding.get("flagged"):
                    taint_reason = "screener:" + (",".join(finding.get("filters", [])) or self.screener.name)
                    self.audit.append({"type": "screening_flagged", "jti": claims["jti"], "tool": tool,
                                       "screener": self.screener.name, "filters": finding.get("filters", []),
                                       "error": finding.get("error")})
            if taint_reason:
                self.state.set(f"taint:{claims['jti']}", taint_reason, max(60.0, claims["exp"] - self.clock.now()),
                               self.clock.now())
            return self._ok(rid, result, claims, tool, args, "allowed")
        except Denied as d:
            self.audit.append({"type": "decision", "decision": "denied", "code": d.code, "detail": d.detail,
                               "jti": claims and claims.get("jti"), "agent": claims and claims.get("sub"),
                               "tool": tool, "args_hash": sha256_hex(canonical_json(args))})
            resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": d.code, "data": d.detail}}
            return resp, {"decision": "denied", "code": d.code, "close": True}

    def _ok(self, rid, result, claims, tool, args, decision, escrow_id=None):
        self.audit.append({"type": "decision", "decision": decision, "jti": claims["jti"], "agent": claims["sub"],
                           "tool": tool, "args_hash": sha256_hex(canonical_json(args)), "escrow_id": escrow_id,
                           "tier": claims["tier"]})
        return ({"jsonrpc": "2.0", "id": rid, "result": result},
                {"decision": decision, "code": None, "close": False, "escrow_id": escrow_id})

    # ------------------------------------------------------------------ escrow callback
    def execute_approved(self, item: dict) -> dict:
        """Called by the escrow service once enough distinct people have approved."""
        claims = item["claims"]
        try:
            self._check_temporal(claims, item["tool"], item["args"])  # state may have changed meanwhile
        except Denied as d:
            self.audit.append({"type": "escrow_execution_denied", "escrow_id": item["id"], "code": d.code})
            return {"status": "denied", "code": d.code}
        result = self.tools.call(item["tool"], item["args"], claims)
        self._commit_temporal(claims, item["tool"], item["args"])
        self.audit.append({"type": "escrow_executed", "escrow_id": item["id"], "tool": item["tool"],
                           "approvers": item["approvals"]})
        return {"status": "executed", "result": result}


def parse_json(body: bytes) -> dict:
    try:
        return json.loads(body or b"{}")
    except json.JSONDecodeError:
        return {}
