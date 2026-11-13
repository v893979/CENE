"""Scenario runner: replays a use case end to end and checks every expected outcome.

    python -m harness.runner                     # all scenarios, summary table
    python -m harness.runner uc01 uc04 -v        # selected scenarios, step-by-step output
    python -m harness.runner --report out.md     # write a Markdown report

Each scenario YAML has: contracts, data (generator specs), lineage, documents (optional) and
steps. Step types are documented in docs/05_Test_Scenarios.md section 2.
"""
from __future__ import annotations

import argparse
import copy
import sys
import time
from pathlib import Path

import yaml

from cene.audit import verify_records
from cene.contracts import ContractRegistry, from_dict
from cene.escrow import EscrowError
from cene.modes import MODE_AUD, MODE_TYP
from cene.platform import Settings, build_platform
from cene.tokens import TokenError, issue
from cene.util import Clock, b64url, b64url_decode

from .datagen import generate
from .faults import apply as apply_fault, make_rng

HERE = Path(__file__).parent
SCENARIOS = HERE / "scenarios"


class StepFailed(AssertionError):
    pass


def _check(cond: bool, msg: str):
    if not cond:
        raise StepFailed(msg)


class ScenarioRun:
    def __init__(self, scenario: dict, verbose: bool = False):
        self.sc = scenario
        self.verbose = verbose
        self.clock = Clock(start=1_800_000_000.0)
        contracts = ContractRegistry([from_dict(c) for c in scenario["contracts"]])
        # The marker screener stands in for an AI firewall (e.g. Model Armor) in local tests.
        self.p = build_platform(Settings(mode="local", screener=scenario.get("screener", "marker")), contracts,
                                self.clock, scenario.get("documents"))
        self.tokens: dict[str, str] = {}
        self.claims: dict[str, dict] = {}
        self.binding_sets: dict[str, dict] = {}  # token alias -> {tool: [{binding, proof}]}
        self.last_escrow: str | None = None
        self.rid = 0
        self.rng = make_rng(int(scenario.get("seed", 11)))
        self.log: list[str] = []
        self._seed()

    # ------------------------------------------------------------------ setup
    def _seed(self):
        now = self.clock.now()
        for prod, spec in self.sc["data"].items():
            seed = int(spec.get("seed", 1))
            df = generate(spec["columns"], int(spec["rows"]), seed)
            self.p.data.put_table(prod, df, now - float(spec.get("load_age_seconds", 60)))
            ref_rows = int(spec.get("reference_rows", 1000))
            self.p.data.put_reference(prod, "baseline", generate(spec["columns"], ref_rows, seed + 1000, start=500000))
            for name, prof in (spec.get("profiles") or {}).items():
                self.p.data.put_reference(prod, name, generate(spec["columns"], ref_rows, seed + 2000,
                                                               start=600000, overrides=prof.get("overrides")))
        for node, h in (self.sc.get("lineage") or {}).items():
            self.p.data.set_lineage_health(node, h)

    # ------------------------------------------------------------------ helpers
    def _next_id(self):
        self.rid += 1
        return f"req-{self.rid}"

    def _binds(self, token: str, tool: str) -> list[dict]:
        spec = self.claims[token]["tools"].get(tool, {})
        if "bindings" in spec:
            return spec["bindings"]
        return [e["binding"] for e in self.binding_sets.get(token, {}).get(tool, [])]

    def _binding_args(self, spec: dict) -> dict:
        binds = self._binds(spec["token"], spec["tool"])
        where = spec.get("binding_where") or {}
        binds = [b for b in binds if all(b["k"].get(k) == v for k, v in where.items())]
        _check(len(binds) > int(spec.get("binding", 0)), f"not enough bindings for {spec['tool']}")
        b = binds[int(spec.get("binding", 0))]
        args = dict(b["k"])
        for a, m in b.get("max", {}).items():
            frac = float(spec.get("amount_fraction", 1.0))
            args[a] = round(m * frac, 2)
        return args

    def _call(self, spec: dict, token: str | None) -> tuple[dict, dict]:
        if "from_binding" in spec:
            args = self._binding_args({**spec["from_binding"], "token": spec.get("token"), "tool": spec["tool"]})
        elif "from_row" in spec:  # take arguments from a row of the data product (e.g. an unscanned order)
            fr = spec["from_row"]
            df = self.p.data.tables[fr["product"]]
            for c in fr.get("where_null", []):
                df = df[df[c].isna()]
            for c, v in (fr.get("where") or {}).items():
                df = df[df[c] == v]
            _check(len(df) > 0, "no row matches from_row")
            row = df.iloc[0]
            args = {a: (row[c].item() if hasattr(row[c], "item") else row[c]) for a, c in fr["fields"].items()}
        else:
            args = {}
        args.update(spec.get("args") or {})
        for k, v in (spec.get("override") or {}).items():
            args[k] = v
        if "copy_from_binding" in spec:  # e.g. take account_id from a different binding (swap test)
            other = self._binding_args({**spec["copy_from_binding"], "token": spec.get("token"), "tool": spec["tool"]})
            for k in spec["copy_from_binding"]["fields"]:
                args[k] = other[k]
        req = {"jsonrpc": "2.0", "id": spec.get("id") or self._next_id(), "method": "tools/call",
               "params": {"name": spec["tool"], "arguments": args}}
        for e in self.binding_sets.get(spec.get("token"), {}).get(spec["tool"], []):  # attach proof, as an agent would
            if all(args.get(k) == v for k, v in e["binding"]["k"].items()):
                req["params"]["_meta"] = {"binding_proof": e}
                break
        resp, dec = self.p.gateway.handle(token, req)
        if dec.get("escrow_id"):
            self.last_escrow = dec["escrow_id"]
        return resp, dec

    def _expect_decision(self, dec: dict, exp: dict, label: str):
        if "decision" in exp:
            _check(dec["decision"] == exp["decision"], f"{label}: expected {exp['decision']}, got "
                   f"{dec['decision']} ({dec.get('code')})")
        if "code" in exp:
            _check(dec.get("code") == exp["code"], f"{label}: expected code {exp['code']}, got {dec.get('code')}")
        if "decision_in" in exp:
            _check(dec["decision"] in exp["decision_in"], f"{label}: {dec['decision']} not in {exp['decision_in']}")

    # ------------------------------------------------------------------ steps
    def step(self, st: dict) -> str:
        kind = st["do"]
        exp = st.get("expect", {})
        if kind == "evaluate":
            r = self.p.quality.evaluate(self.p.contracts.get(st["product"]))
            if "tier" in exp:
                _check(r.tier == exp["tier"], f"tier {r.tier} != {exp['tier']} (score {r.score}, {r.reasons})")
            if "tier_in" in exp:
                _check(r.tier in exp["tier_in"], f"tier {r.tier} not in {exp['tier_in']} ({r.reasons})")
            for k, v in (exp.get("vector_below") or {}).items():
                _check(r.vectors[k] < v, f"{k}={r.vectors[k]} not below {v}")
            for k, v in (exp.get("vector_above") or {}).items():
                _check(r.vectors[k] > v, f"{k}={r.vectors[k]} not above {v}")
            if "linear_cqi_above" in exp:
                _check(r.linear_cqi > exp["linear_cqi_above"], f"linear CQI {r.linear_cqi} not above "
                       f"{exp['linear_cqi_above']}")
            if "reason_contains" in exp:
                _check(any(exp["reason_contains"] in x for x in r.reasons), f"no reason with "
                       f"'{exp['reason_contains']}' in {r.reasons}")
            return (f"tier {r.tier} | score {r.score} | d-CQI {r.dcqi} | gate {r.gate} | linear CQI "
                    f"{r.linear_cqi} | mode {r.mode} | " + (", ".join(r.reasons) or "no issues"))
        if kind == "issue":
            out = self.p.control.issue(st.get("agent", "agent-1"), st.get("task", "task"), st["products"],
                                       st.get("tools"))
            self.tokens[st["as"]], self.claims[st["as"]] = out["token"], out["claims"]
            self.binding_sets[st["as"]] = out.get("bindings", {})
            c = out["claims"]
            if "tier" in exp:
                _check(c["tier"] == exp["tier"], f"token tier {c['tier']} != {exp['tier']}")
            for t in exp.get("has_tools", []):
                _check(t in c["tools"], f"token lacks tool {t}")
            for t in exp.get("lacks_tools", []):
                _check(t not in c["tools"], f"token unexpectedly has {t}")
            for t, m in (exp.get("modes") or {}).items():
                _check(c["tools"][t]["mode"] == m, f"{t} mode {c['tools'][t]['mode']} != {m}")
            if "ttl_below" in exp:
                ttl = c["exp"] - c["iat"]
                _check(ttl < exp["ttl_below"], f"token ttl {ttl:.1f}s not below {exp['ttl_below']}")
            return f"token {st['as']}: tier {c['tier']}, tools {sorted(c['tools'])}, ttl {c['exp'] - c['iat']:.0f}s"
        if kind == "call":
            resp, dec = self._call(st, self.tokens.get(st.get("token")) if st.get("token") else None)
            self._expect_decision(dec, exp, "call")
            return f"{st['tool']} -> {dec['decision']}" + (f" ({dec['code']})" if dec.get("code") else "")
        if kind == "call_until_denied":
            allowed = 0
            binds = [b for b in self._binds(st["token"], st["tool"])
                     if all(b["k"].get(k) == v for k, v in (st.get("binding_where") or {}).items())]
            last = None
            for i in range(min(len(binds), int(st.get("max_calls", 50)))):
                spec = {**st, "from_binding": {"binding": i, "binding_where": st.get("binding_where"),
                                               "amount_fraction": st.get("amount_fraction", 1.0)}}
                _, dec = self._call(spec, self.tokens[st["token"]])
                last = dec
                if dec["decision"] != "allowed":
                    break
                allowed += 1
            _check(last is not None and last["decision"] == "denied", "never denied")
            _check(last.get("code") == exp.get("code", last.get("code")), f"denied with {last.get('code')}")
            _check(allowed >= int(exp.get("min_allowed", 1)), f"only {allowed} allowed before denial")
            return f"{allowed} calls allowed, then denied ({last['code']})"
        if kind == "direct":
            args = self._binding_args({**st["from_binding"], "token": st["token"], "tool": st["tool"]}) \
                if "from_binding" in st else {}
            args.update(st.get("args") or {})
            args.update(st.get("override") or {})
            before = self.p.tools.executed_count(st["tool"])
            for _ in range(int(st.get("repeat", 1))):
                self.p.tools.call(st["tool"], args, {"sub": "agent-without-controls"})
            n = self.p.tools.executed_count(st["tool"]) - before
            return f"WITHOUT CONTROLS: {st['tool']} executed {n} time(s) with {args}"
        if kind == "approve" or kind == "reject":
            eid = self.last_escrow if st.get("escrow", "last") == "last" else st["escrow"]
            try:
                fn = self.p.escrow.approve if kind == "approve" else self.p.escrow.reject
                out = fn(eid, st["approver"])
                status, code = out["status"], out.get("code")
            except EscrowError as e:
                status, code = "error", e.code
            if "status" in exp:
                _check(status == exp["status"], f"escrow status {status} != {exp['status']} ({code})")
            if "code" in exp:
                _check(code == exp["code"], f"escrow code {code} != {exp['code']}")
            return f"{kind} by {st['approver']} -> {status}" + (f" ({code})" if code else "")
        if kind == "mode_switch":
            signer = {"oracle": self.p.oracle_signer, "token": self.p.token_signer}[st.get("signer", "oracle")]
            now = self.clock.now()
            stmt = issue({"aud": MODE_AUD, "product": st["product"], "mode": st["mode"], "iat": now, "nbf": now,
                          "exp": now + float(st.get("ttl", 3600)), "reason": st.get("reason", "")}, signer,
                         typ=MODE_TYP)
            try:
                self.p.modes.switch(stmt)
                result = "ok"
            except TokenError as e:
                result = e.code
            _check(result == exp.get("result", "ok"), f"mode switch {result} != {exp.get('result', 'ok')}")
            return f"mode switch to '{st['mode']}' signed by {st.get('signer', 'oracle')} key -> {result}"
        if kind == "delegate":
            try:
                out = self.p.control.delegate(self.tokens[st["parent"]], self.p.keyring, st["agent"], st["tools"])
                self.tokens[st["as"]], self.claims[st["as"]] = out["token"], out["claims"]
                # Tools whose bindings were not narrowed keep the parent's Merkle root, so the
                # parent's proofs remain valid for the child.
                sets = dict(out.get("bindings", {}))
                parent_sets = self.binding_sets.get(st["parent"], {})
                for tool, spec in out["claims"]["tools"].items():
                    parent_root = self.claims[st["parent"]]["tools"].get(tool, {}).get("bindings_root")
                    if tool not in sets and spec.get("bindings_root") and spec["bindings_root"] == parent_root:
                        sets[tool] = parent_sets.get(tool, [])
                self.binding_sets[st["as"]] = sets
                result = "ok"
            except TokenError as e:
                result = e.code
            _check(result == exp.get("result", "ok"), f"delegation {result} != {exp.get('result', 'ok')}")
            return f"delegate {st['parent']} -> {st.get('as', '?')} ({st['agent']}): {result}"
        if kind == "tamper_token":
            h, pl, sg = self.tokens[st["from"]].split(".")
            import json as _j

            claims = _j.loads(b64url_decode(pl))
            claims.update(st.get("set", {}))
            self.tokens[st["as"]] = h + "." + b64url(_j.dumps(claims).encode()) + "." + sg
            self.claims[st["as"]] = claims
            self.binding_sets[st["as"]] = self.binding_sets.get(st["from"], {})
            return f"tampered copy of {st['from']} as {st['as']}"
        if kind == "forge_token":
            forged = issue({**self.claims[st["from"]], "jti": "forged"}, self.p.oracle_signer)
            self.tokens[st["as"]], self.claims[st["as"]] = forged, self.claims[st["from"]]
            self.binding_sets[st["as"]] = self.binding_sets.get(st["from"], {})
            return f"token re-signed with an untrusted key as {st['as']}"
        if kind == "fault":
            return "fault: " + apply_fault(st["fault"], self.p.data, self.sc, self.clock, self.rng)
        if kind == "advance":
            self.clock.advance(float(st["seconds"]))
            return f"clock +{st['seconds']}s"
        if kind == "assert_executed":
            n = self.p.tools.executed_count(st["tool"])
            _check(n == exp["count"], f"{st['tool']} executed {n} times, expected {exp['count']}")
            return f"{st['tool']} executed {n} time(s) in total"
        if kind == "audit_verify":
            self.p.audit.checkpoint()
            ok, msg = verify_records(self.p.audit_sink.records, self.p.audit_signer.public_pem())
            _check(ok == exp.get("valid", True), f"audit verification {ok} ({msg})")
            return f"audit chain verification: {'valid' if ok else 'INVALID'} - {msg}"
        if kind == "audit_tamper":
            recs = self.p.audit_sink.records
            ev = next(r for r in recs if r["kind"] == "event" and r["event"].get("type") == "decision")
            ev["event"]["decision"] = "allowed" if ev["event"]["decision"] != "allowed" else "denied"
            return f"edited audit record seq {ev['seq']}"
        raise ValueError(f"unknown step {kind}")

    def run(self) -> dict:
        results = []
        for i, st in enumerate(self.sc["steps"], 1):
            label = st.get("name", st["do"])
            try:
                msg = self.step(st)
                results.append({"n": i, "name": label, "ok": True, "msg": msg})
            except StepFailed as e:
                results.append({"n": i, "name": label, "ok": False, "msg": str(e)})
            if self.verbose:
                r = results[-1]
                print(f"  {'PASS' if r['ok'] else 'FAIL'} {i:2d}. {label}: {r['msg']}")
        return {"id": self.sc["id"], "title": self.sc["title"], "steps": results,
                "passed": all(r["ok"] for r in results)}


def load_scenarios(ids: list[str] | None = None) -> list[dict]:
    out = []
    for path in sorted(SCENARIOS.glob("uc*.yaml")):
        sc = yaml.safe_load(path.read_text())
        if not ids or sc["id"] in ids or path.stem in ids:
            out.append(sc)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--report")
    a = ap.parse_args(argv)
    all_res, t0 = [], time.time()
    for sc in load_scenarios(a.ids):
        if a.verbose:
            print(f"\n=== {sc['id']}: {sc['title']} ===")
        res = ScenarioRun(copy.deepcopy(sc), a.verbose).run()
        all_res.append(res)
        if not a.verbose:
            n_ok = sum(r["ok"] for r in res["steps"])
            print(f"{'PASS' if res['passed'] else 'FAIL'}  {res['id']}  {n_ok}/{len(res['steps'])} steps  {res['title']}")
    if a.report:
        lines = ["# Scenario report", "", f"Run time {time.time() - t0:.1f}s", ""]
        for res in all_res:
            lines += [f"## {res['id']} - {res['title']} - {'PASS' if res['passed'] else 'FAIL'}", "",
                      "| # | Step | Result | Detail |", "|---|---|---|---|"]
            for r in res["steps"]:
                lines.append(f"| {r['n']} | {r['name']} | {'pass' if r['ok'] else 'FAIL'} | "
                             f"{r['msg'].replace('|', '/')} |")
            lines.append("")
        Path(a.report).write_text("\n".join(lines))
    return 0 if all(r["passed"] for r in all_res) else 1


if __name__ == "__main__":
    sys.exit(main())
