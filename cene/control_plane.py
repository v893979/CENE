"""Control plane: evaluates data quality and issues signed Context Tokens.

issue()    - one token per agent task, scoped to the data products the task needs.
delegate() - a narrower child token for a sub-agent (transitive attenuation).

Tier -> capabilities (per data product, worst tier wins):
  Tier 1  read tools + write tools executed directly
  Tier 2  read tools; write tools go to two-person escrow
  Tier 3  no tools (circuit breaker)
Token expiry = min(default TTL, time until the stalest product falls below its freshness gate).
"""
from __future__ import annotations

import uuid

from . import merkle
from .tokens import TokenError, issue, token_hash, verify

AUDIENCE = "cene-gateway"
ISSUER = "cene-control-plane"
MAX_DELEGATION_DEPTH = 2
MAX_BINDINGS = 5000          # rows considered per tool (the sample size)
INLINE_BINDINGS_MAX = 20     # above this, the token carries a Merkle root instead of the list


def _arg_spec(spec, limits: dict, name: str):
    """Normalise an argument spec and fold tool limits (e.g. amount_max) into numeric bounds."""
    if isinstance(spec, str):
        return {"pattern": spec}
    s = dict(spec)
    lim = limits.get(f"{name}_max")
    if lim is not None:
        s["max"] = min(float(s.get("max", lim)), float(lim))
    return s


def _key_value(v):
    """A binding key as a plain JSON value; a missing cell (None / NaN) becomes null, so it matches an unset
    nullable argument (pandas turns None into NaN, and NaN never equals anything)."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    v = v.item() if hasattr(v, "item") else v
    return None if isinstance(v, float) and v != v else v


class ControlPlane:
    def __init__(self, contracts, quality, store, signer, clock, audit, default_ttl: float = 900):
        self.contracts = contracts
        self.quality = quality
        self.store = store  # data store (for bindings)
        self.signer = signer
        self.clock = clock
        self.audit = audit
        self.default_ttl = default_ttl

    # ------------------------------------------------------------------ bindings
    def _bindings(self, product: str, bind: dict, flagged: set) -> list[dict]:
        df = self.store.sample(product)
        if flagged:
            df = df.drop(index=[i for i in flagged if i in df.index])
        for col, val in (bind.get("where") or {}).items():
            df = df[df[col] == val]
        for col in bind.get("require_not_null", []):
            df = df[df[col].notna() & (df[col].astype(str).str.strip() != "")]
        keys, maxes = bind["keys"], bind.get("max", {})
        out = []
        for _, row in df.head(MAX_BINDINGS).iterrows():
            out.append({"k": {k: _key_value(row[k]) for k in keys},
                        "max": {a: float(row[col]) for a, col in maxes.items()}})
        return out

    # ------------------------------------------------------------------ issue
    def issue(self, agent_id: str, task: str, products: list[str], requested_tools: list[str] | None = None,
              requested_by: str | None = None) -> dict:
        """requested_by: the authenticated caller (Cloud Run identity token email). agent_id is only a label
        the caller chose, so escrow also refuses requested_by as an approver."""
        now = self.clock.now()
        reports = {p: self.quality.evaluate(self.contracts.get(p)) for p in products}
        tier = max(r.tier for r in reports.values())
        tools, temporal, untrusted, approvers, on_taint = {}, [], set(), 2, "escrow"
        binding_sets: dict[str, list] = {}
        for p in products:
            c, r = self.contracts.get(p), reports[p]
            approvers = max(approvers, int(c.escrow.get("approvers_required", 2)))
            untrusted |= set(c.taint.get("untrusted_tools", []))
            on_taint = c.taint.get("on_taint", on_taint)
            if tier >= 3:
                continue
            for name, spec in (c.tools.get("read") or {}).items():
                tools[name] = {"kind": "read", "mode": "direct", "product": p,
                               "args": {a: _arg_spec(s, {}, a) for a, s in (spec or {}).get("args", {}).items()}}
            for name, spec in (c.tools.get("write") or {}).items():
                limits = spec.get("limits", {})
                direct = tier == 1 and not spec.get("always_escrow", False)
                entry = {"kind": "write", "mode": "direct" if direct else "escrow", "product": p,
                         "args": {a: _arg_spec(s, limits, a) for a, s in spec.get("args", {}).items()}}
                on_unbound = spec.get("on_unbound", "deny")
                if on_unbound not in ("deny", "escrow"):
                    raise ValueError(f"{p}.{name}: on_unbound must be deny or escrow")
                if on_unbound == "escrow":
                    entry["on_unbound"] = "escrow"
                if "bind" in spec:
                    self._attach_bindings(entry, name, self._bindings(p, spec["bind"], set(r.flagged_rows)),
                                          binding_sets)
                tools[name] = entry
            temporal += [t for t in c.temporal]
        if requested_tools is not None:  # least privilege: only what the task asked for
            tools = {k: v for k, v in tools.items() if k in requested_tools}
            binding_sets = {k: v for k, v in binding_sets.items() if k in tools}
        temporal = [t for t in temporal if t["tool"] in tools]
        # t_expiry: Tier-1 (direct) rights must not outlive the data's freshness. Tier 2/3 tokens only
        # carry read or escrow rights, so they use the default lifetime.
        ttl = self.default_ttl
        if tier == 1:
            ttl = min([ttl] + [r.seconds_until_stale for r in reports.values()])
        claims = {
            "iss": ISSUER, "aud": AUDIENCE, "sub": agent_id, "jti": uuid.uuid4().hex,
            "iat": round(now, 3), "nbf": round(now, 3), "exp": round(now + max(0.0, ttl), 3),
            "task": task, "tier": tier,
            "products": {p: {"score": r.score, "tier": r.tier, "mode": r.mode} for p, r in reports.items()},
            "tools": tools, "temporal": temporal,
            "taint": {"untrusted_tools": sorted(untrusted), "on_taint": on_taint},
            "escrow": {"approvers_required": approvers},
            "depth": 0, "parent": None,
        }
        if requested_by:
            claims["req_by"] = requested_by
        tok = issue(claims, self.signer)
        self.audit.append({"type": "token_issued", "jti": claims["jti"], "agent": agent_id, "req_by": requested_by,
                           "tier": tier,
                           "tools": sorted(tools), "exp": claims["exp"],
                           "products": claims["products"],
                           "reasons": {p: r.reasons for p, r in reports.items()}})
        return {"token": tok, "claims": claims, "bindings": binding_sets,
                "reports": {p: r.to_dict() for p, r in reports.items()}}

    @staticmethod
    def _attach_bindings(entry: dict, tool: str, binds: list[dict], binding_sets: dict) -> None:
        """Small sets go inline; large sets become a signed Merkle root plus per-row proofs for the agent."""
        if len(binds) <= INLINE_BINDINGS_MAX:
            entry["bindings"] = binds
            return
        root, proofs = merkle.build(binds)
        entry["bindings_root"], entry["bindings_count"] = root, len(binds)
        binding_sets[tool] = [{"binding": b, "proof": p} for b, p in zip(binds, proofs)]

    # ------------------------------------------------------------------ delegate
    def delegate(self, parent_token: str, keyring, child_agent: str, tools: dict, ttl: float | None = None) -> dict:
        """Issue a child token whose rights are a subset of the parent's.

        tools = {tool_name: {"limits": {"amount_max": 15}, "bindings_subset": [binding, ...]}}
        Allowed narrowing: drop tools, lower numeric maxima, keep a subset of bindings.
        Escrow tools stay escrow; argument patterns are inherited unchanged.
        """
        now = self.clock.now()
        parent = verify(parent_token, keyring, now, AUDIENCE)
        if parent.get("depth", 0) >= MAX_DELEGATION_DEPTH:
            raise TokenError("DELEGATION_TOO_DEEP")
        child_tools, child_sets = {}, {}
        for name, narrowing in tools.items():
            if name not in parent["tools"]:
                raise TokenError("DELEGATION_ESCALATION", f"parent has no tool '{name}'")
            t = {**parent["tools"][name], "args": {k: dict(v) for k, v in parent["tools"][name]["args"].items()}}
            for key, val in (narrowing or {}).get("limits", {}).items():
                arg = key[:-4] if key.endswith("_max") else key
                spec = t["args"].get(arg)
                if spec is None or "max" not in spec and spec.get("type") != "number":
                    raise TokenError("DELEGATION_ESCALATION", f"cannot limit '{arg}'")
                if "max" in spec and float(val) > float(spec["max"]):
                    raise TokenError("DELEGATION_ESCALATION", f"{arg} limit above parent")
                spec["max"] = float(val)
            if "bindings_subset" in (narrowing or {}):
                sub = narrowing["bindings_subset"]
                if "bindings_root" in t:  # parent committed by Merkle root: each entry needs its proof
                    if any(not merkle.verify(e.get("binding"), e.get("proof"), t["bindings_root"]) for e in sub):
                        raise TokenError("DELEGATION_ESCALATION", "binding not held by parent")
                    plain = [e["binding"] for e in sub]
                else:
                    if any(b not in t.get("bindings", []) for b in sub):
                        raise TokenError("DELEGATION_ESCALATION", "binding not held by parent")
                    plain = sub
                t.pop("bindings_root", None)
                t.pop("bindings_count", None)
                t.pop("bindings", None)
                self._attach_bindings(t, name, plain, child_sets)
            child_tools[name] = t
        exp = min(float(parent["exp"]), now + (ttl or self.default_ttl))
        claims = {**parent, "sub": child_agent, "jti": uuid.uuid4().hex, "iat": round(now, 3),
                  "nbf": round(now, 3), "exp": round(exp, 3), "tools": child_tools,
                  "temporal": [t for t in parent["temporal"] if t["tool"] in child_tools],
                  "depth": parent.get("depth", 0) + 1, "parent": token_hash(parent_token)}
        tok = issue(claims, self.signer)
        self.audit.append({"type": "token_delegated", "jti": claims["jti"], "parent_jti": parent["jti"],
                           "agent": child_agent, "tools": sorted(child_tools), "depth": claims["depth"]})
        return {"token": tok, "claims": claims, "bindings": child_sets}
