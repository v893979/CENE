"""Run AgentDojo tool calls through CENE's real gateway, in-process.

    pipeline = AgentPipeline([SystemMessage(m), InitQuery(), CeneSession(), llm,
                              ToolsExecutionLoop([ToolsExecutor(), llm])])

CeneSession (a pipeline element) runs once per episode, before the first model call. From the user prompt
and the trusted environment fields it builds the per-task data products (cene_agentdojo/policy.py), builds a
real CENE platform (ControlPlane, QualityEngine, Gateway, EscrowService, AuditLog, LocalSigner keys), has
the control plane issue a Context Token, and swaps the FunctionsRuntime for a CeneRuntime.

CeneRuntime.run_function validates the arguments exactly as AgentDojo does (so optional arguments get their
defaults and malformed calls fail the same way), then sends a JSON-RPC tools/call to Gateway.handle(). The
gateway's tool backend executes the real AgentDojo function only if the gateway lets the call through.

  allowed  -> AgentDojo's normal result
  denied   -> ("", "CENE_DENIED <CODE>: <detail>"), AgentDojo's normal tool-error path; the agent continues
  escrowed -> not executed; a "held for approval" result; nobody approves during the episode
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionsRuntime

from cene.audit import verify_records
from cene.platform import Settings, build_platform
from cene.util import Clock

from . import policy

AGENT_ID = "agentdojo-agent"
REQUESTED_BY = "agentdojo-user"
WRITE_TOOLS = ("send_money", "schedule_transaction", "update_scheduled_transaction", "update_password",
               "update_user_info")
HELD_TEXT = ("Held for approval (escrow {id}): this action was not executed. It needs approval by two people "
             "other than the requester.")


class _ToolFailed(Exception):
    pass


class AgentDojoToolBackend:
    """Duck-types cene.tools.ToolService.call(): runs the real AgentDojo function on the live environment."""

    def __init__(self, inner: FunctionsRuntime):
        self.inner = inner
        self.env = None
        self.last: tuple | None = None
        self.executed: list[dict] = []

    def call(self, tool: str, args: dict, claims: dict | None = None) -> dict:
        value, error = self.inner.run_function(self.env, tool, args, raise_on_error=False)
        self.last = (value, error)
        if error is not None:  # the tool itself failed: the gateway records tool_error, no temporal commit
            raise _ToolFailed(error)
        self.executed.append({"tool": tool, "args": dict(args)})
        return {"status": "ok"}


class CeneRuntime(FunctionsRuntime):
    def __init__(self, inner: FunctionsRuntime, session: "CeneSession"):
        super().__init__(list(inner.functions.values()))
        self.inner = inner
        self.session = session
        self.backend = session.platform.gateway.tools
        self._rid = 0

    def run_function(self, env, function: str, kwargs: Mapping, raise_on_error: bool = False):
        f = self.inner.functions.get(function)
        if f is None:
            return self.inner.run_function(env, function, kwargs, raise_on_error)  # AgentDojo's own error
        try:
            args = f.parameters.model_validate(dict(kwargs)).model_dump()
        except ValidationError:
            return self.inner.run_function(env, function, kwargs, raise_on_error)  # identical error message
        self._rid += 1
        request = {"jsonrpc": "2.0", "id": self._rid, "method": "tools/call",
                   "params": {"name": function, "arguments": args}}
        proof = self.session.binding_proof(function, args)
        if proof is not None:
            request["params"]["_meta"] = {"binding_proof": proof}
        self.backend.env = env
        self.backend.last = None
        response, decision = self.session.platform.gateway.handle(self.session.token, request)
        rec = {"tool": function, "args": args, "decision": decision["decision"], "code": decision.get("code"),
               "escrow_id": decision.get("escrow_id"), "write": function in WRITE_TOOLS}
        if decision["decision"] == "escrow":
            rec["reason"] = self.session.platform.escrow.items[decision["escrow_id"]]["reason"]
        self.session.log(rec)
        d = decision["decision"]
        if d == "allowed":
            return self.backend.last
        if d == "escrow":
            return HELD_TEXT.format(id=decision["escrow_id"]), None
        if d == "error":  # the AgentDojo function raised: same result as without CENE
            value, error = self.backend.last
            if raise_on_error:
                raise RuntimeError(error)
            return value, error
        err = response.get("error") or {}
        return "", f"CENE_DENIED {err.get('message')}: {err.get('data') or ''}".rstrip(": ")


class CeneSession(BasePipelineElement):
    """Issues one Context Token per episode and installs the CeneRuntime."""

    name = "cene"

    def __init__(self, clock_start: float = 1_800_000_000.0):
        self.clock_start = clock_start
        self.episodes: list[dict] = []   # one entry per token issued (AgentDojo may retry an episode)
        self.platform = None
        self.token = None
        self._proofs: dict = {}

    # ---------------------------------------------------------------- per-episode set-up
    def query(self, query: str, runtime: FunctionsRuntime, env: Env = EmptyEnv(),
              messages: Sequence = [], extra_args: dict = {}):
        inner = runtime.inner if isinstance(runtime, CeneRuntime) else runtime
        facts = policy.extract(query, env)
        contracts = policy.contracts_for(facts)
        tables = policy.tables_for(facts)
        clock = Clock(start=self.clock_start)
        p = build_platform(Settings(mode="local", screener="none"), contracts, clock)
        p.tools = p.gateway.tools = AgentDojoToolBackend(inner)
        now = clock.now()
        products = []
        for name, df in tables.items():
            if df.empty:
                continue
            p.data.put_table(name, df, now)
            p.data.put_reference(name, "baseline", df)
            products.append(name)
        out = p.control.issue(AGENT_ID, hashlib.sha256(query.encode()).hexdigest()[:16], products,
                              requested_by=REQUESTED_BY)
        self.platform, self.token, self._proofs = p, out["token"], out["bindings"]
        claims = out["claims"]
        self.episodes.append({
            "policy_version": policy.POLICY_VERSION, "facts": facts.to_dict(), "products": products,
            "tier": claims["tier"], "scores": {k: v["score"] for k, v in claims["products"].items()},
            "reasons": {k: r["reasons"] for k, r in out["reports"].items()},
            "tools": sorted(claims["tools"]),
            "bindings": {t: s.get("bindings", s.get("bindings_count")) for t, s in claims["tools"].items()
                         if "bindings" in s or "bindings_count" in s},
            "calls": [],
        })
        return query, CeneRuntime(inner, self), env, messages, extra_args

    def binding_proof(self, tool: str, args: dict):
        """For Merkle-committed bindings, attach the proof of the binding the call uses, if there is one."""
        for entry in self._proofs.get(tool, []):
            if all(args.get(k) == v for k, v in entry["binding"]["k"].items()):
                return entry
        return None

    def log(self, rec: dict) -> None:
        self.episodes[-1]["calls"].append(rec)

    # ---------------------------------------------------------------- results
    def finish(self) -> dict:
        """Summary of the last episode (all tokens since the last reset()), with the audit chain checked."""
        audit_ok = None
        if self.platform is not None:
            self.platform.audit.close()  # sign the tail so the whole chain is covered
            ok, msg = verify_records(self.platform.audit_sink.records, self.platform.audit_signer.public_pem())
            audit_ok = ok if ok else msg
        calls = [c for e in self.episodes for c in e["calls"]]
        return {"tokens": self.episodes, "audit_chain_ok": audit_ok, "retries": max(0, len(self.episodes) - 1),
                **summarise(calls)}

    def reset(self) -> None:
        self.episodes, self.platform, self.token, self._proofs = [], None, None, {}


def summarise(calls: list[dict]) -> dict:
    writes = [c for c in calls if c["write"]]
    return {
        "calls": len(calls),
        "allowed": sum(c["decision"] == "allowed" for c in calls),
        "denied": sum(c["decision"] == "denied" for c in calls),
        "held": sum(c["decision"] == "escrow" for c in calls),
        "tool_errors": sum(c["decision"] == "error" for c in calls),
        "write_allowed": sum(c["decision"] == "allowed" for c in writes),
        "write_denied": sum(c["decision"] == "denied" for c in writes),
        "write_held": sum(c["decision"] == "escrow" for c in writes),
        "denied_codes": sorted({c["code"] for c in calls if c["decision"] == "denied"}),
        "held_reasons": sorted({c.get("reason") for c in calls if c["decision"] == "escrow"}),
    }
