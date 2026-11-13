"""Summarise G1/G2 (model-free) into docs/results/agentdojo/g1_g2.json.

    python -m cene_agentdojo.summarise_g RAW.json docs/results/agentdojo/g1_g2.json

Analysis code written after the policy-v1 tag; it changes no policy. It also runs a control (G0): the same
ground truth through AgentDojo's plain FunctionsRuntime, without CENE, so CENE's effect can be separated from
AgentDojo's own utility/security checks.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

from .hijack import STATE_CHANGING


def wilson(k: int, n: int, z: float = 1.959964) -> list | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def _calls(ep):
    return [c for t in ep["cene"]["tokens"] for c in t["calls"]]


def _label(c, tier):
    if c["decision"] == "escrow":
        r = c.get("reason")
        # CENE labels every mode=escrow tool "tier2"; with a Tier-1 token that means the contract's always_escrow
        return "held:always_escrow" if r == "tier2" and tier == 1 else f"held:{r}"
    if c["decision"] == "denied":
        return f"denied:{c['code']}"
    return c["decision"]


def control(suite):
    """G0: ground truth through AgentDojo's own runtime, no CENE."""
    from agentdojo.agent_pipeline import AgentPipeline
    from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline
    from agentdojo.attacks.attack_registry import load_attack

    from .run import _GTName, _num

    out_u, out_i, trivial = {}, {}, {}
    uts = sorted(suite.user_tasks.values(), key=lambda t: _num(t.ID))
    its = sorted(suite.injection_tasks.values(), key=lambda t: _num(t.ID))
    for ut in uts:
        p = AgentPipeline([GroundTruthPipeline(ut)]); p.name = "local"
        out_u[ut.ID] = bool(suite.run_task_with_pipeline(p, ut, None, {})[0])
        # Does AgentDojo's utility check already pass before the agent does anything?
        pre = ut.init_environment(suite.load_and_inject_default_environment({}))
        trivial[ut.ID] = bool(ut.utility("", pre, pre.model_copy(deep=True)))
    attack = load_attack("important_instructions", suite, _GTName())
    for ut in uts:
        for it in its:
            p = AgentPipeline([GroundTruthPipeline(it)]); p.name = "local"
            out_i[f"{ut.ID}|{it.ID}"] = bool(suite.run_task_with_pipeline(p, ut, it, attack.attack(ut, it))[1])
    return out_u, out_i, trivial


def main(raw_path: str, out_path: str) -> int:
    import warnings
    warnings.filterwarnings("ignore")
    from agentdojo.task_suite.load_suites import get_suite

    raw = json.loads(Path(raw_path).read_text())
    cfg = raw["runs"][0]["run_config"]
    suite = get_suite(cfg["agentdojo"]["benchmark_version"], cfg["agentdojo"]["suite"])
    g0_u, g0_i, trivial = control(suite)
    eps = raw["episodes"]

    # ---------------------------------------------------------------- G1
    g1 = {}
    for k, e in eps.items():
        if not k.startswith("G1|"):
            continue
        tier = e["cene"]["tokens"][-1]["tier"]
        legit = [{"tool": c["tool"], "outcome": _label(c, tier),
                  "args": {a: v for a, v in c["args"].items() if v is not None}}
                 for c in _calls(e) if c["write"]]
        g1[e["user_task"]] = {
            "utility_with_cene": e["utility"], "utility_without_cene_G0": g0_u[e["user_task"]],
            "utility_check_passes_with_no_action": trivial[e["user_task"]],
            "error": e["error"], "token_tier": tier, "writes": legit,
            "any_write_held": any(w["outcome"].startswith("held") for w in legit),
            "any_write_denied": any(w["outcome"].startswith("denied") for w in legit),
            "all_writes_executed": all(w["outcome"] == "allowed" for w in legit),
        }
    n1 = len(g1)
    held_tasks = sum(v["any_write_held"] for v in g1.values())
    denied_tasks = sum(v["any_write_denied"] for v in g1.values())
    clean = sum(v["all_writes_executed"] and v["utility_with_cene"] for v in g1.values())
    g1_codes = Counter(w["outcome"] for v in g1.values() for w in v["writes"] if w["outcome"] != "allowed")

    # ---------------------------------------------------------------- G2
    g2 = {}
    for k, e in eps.items():
        if not k.startswith("G2|"):
            continue
        tier = e["cene"]["tokens"][-1]["tier"]
        bal = e["cene"]["tokens"][-1]["facts"]["B"]
        sc = [{"tool": c["tool"], "outcome": _label(c, tier), "amount": c["args"].get("amount"), "balance_B": bal}
              for c in _calls(e) if c["tool"] in STATE_CHANGING]
        executed = [c for c in sc if c["outcome"] == "allowed"]
        g2[f"{e['user_task']}|{e['injection_task']}"] = {
            "user_task": e["user_task"], "injection_task": e["injection_task"],
            "security_with_cene": e["security"], "security_without_cene_G0": g0_i[f"{e['user_task']}|{e['injection_task']}"],
            "error": e["error"], "token_tier": tier, "attacker_state_changing_calls": sc,
            "any_executed": bool(executed),
            "any_held": any(c["outcome"].startswith("held") for c in sc),
            "any_denied": any(c["outcome"].startswith("denied") for c in sc),
            "all_stopped": bool(sc) and not executed,
            "stopped_by": ("denied" if all(c["outcome"].startswith("denied") for c in sc) else
                           "held" if all(c["outcome"].startswith("held") for c in sc) else "mixed")
            if sc and not executed else None,
        }
    n2 = len(g2)
    by_inj = {}
    for v in g2.values():
        b = by_inj.setdefault(v["injection_task"], {"pairs": 0, "security_with_cene": 0, "all_stopped": 0,
                                                     "stopped_by_denial": 0, "stopped_by_hold": 0, "outcomes": Counter()})
        b["pairs"] += 1
        b["security_with_cene"] += bool(v["security_with_cene"])
        b["all_stopped"] += v["all_stopped"]
        b["stopped_by_denial"] += v["stopped_by"] == "denied"
        b["stopped_by_hold"] += v["stopped_by"] == "held"
        b["outcomes"].update(c["outcome"] for c in v["attacker_state_changing_calls"])
    for b in by_inj.values():
        b["outcomes"] = dict(b["outcomes"])
    g2_codes = Counter(c["outcome"] for v in g2.values() for c in v["attacker_state_changing_calls"])

    summary = {
        "policy_tag": "agentdojo-policy-v1",
        "raw_run": {k: raw["runs"][0][k] for k in ("started", "finished", "git", "agentdojo", "python", "platform")
                    if k in raw["runs"][0]},
        "G1": {
            "what": "every user task's ground truth through CENE (false-block rate)",
            "tasks": n1, "errors": sum(v["error"] is not None for v in g1.values()),
            "utility_with_cene": rate(sum(v["utility_with_cene"] for v in g1.values()), n1),
            "utility_without_cene_G0": rate(sum(g0_u.values()), len(g0_u)),
            "completed_with_every_write_executed": rate(clean, n1),
            "tasks_with_a_write_held": rate(held_tasks, n1),
            "tasks_with_a_write_denied": rate(denied_tasks, n1),
            "tasks_whose_utility_check_passes_with_no_action": sorted(t for t, v in trivial.items() if v),
            "non_allowed_writes_by_outcome": dict(g1_codes),
            "per_task": g1,
        },
        "G2": {
            "what": "every injection task's ground-truth attacker calls through the user task's token, 16 x 9",
            "pairs": n2, "errors": sum(v["error"] is not None for v in g2.values()),
            "attack_goal_met_with_cene": rate(sum(bool(v["security_with_cene"]) for v in g2.values()), n2),
            "attack_goal_met_without_cene_G0": rate(sum(g0_i.values()), len(g0_i)),
            "pairs_all_attacker_writes_stopped": rate(sum(v["all_stopped"] for v in g2.values()), n2),
            "pairs_stopped_by_denial_only": rate(sum(v["stopped_by"] == "denied" for v in g2.values()), n2),
            "pairs_stopped_by_hold_only": rate(sum(v["stopped_by"] == "held" for v in g2.values()), n2),
            "pairs_stopped_mixed": rate(sum(v["stopped_by"] == "mixed" for v in g2.values()), n2),
            "pairs_with_an_attacker_write_executed": rate(sum(v["any_executed"] for v in g2.values()), n2),
            "pairs_with_no_state_changing_attacker_call": sum(not v["attacker_state_changing_calls"] for v in g2.values()),
            "attacker_writes_by_outcome": dict(g2_codes),
            "denied_amounts_vs_balance": sorted({(c["outcome"], c["amount"], c["balance_B"]) for v in g2.values()
                                                 for c in v["attacker_state_changing_calls"]
                                                 if c["outcome"] == "denied:ARG_OUT_OF_BOUNDS"}),
            "by_injection_task": by_inj,
            "per_pair": g2,
        },
    }
    Path(out_path).write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("per_task", "per_pair")}
                      for k, v in summary.items() if k in ("G1", "G2")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
