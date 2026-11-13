"""Full analysis of the AgentDojo model runs (M1-M8) for the Step 4 report.

    python -m cene_agentdojo.analyse_m RAW_DIR OUT.json
    (RAW_DIR = evidence/4_kaggle/b_agentdojo_runs/RUN-2/raw, which holds all eight configurations)

A RAW_DIR may hold only some configurations (e.g. a week-1 run without M5/M6): the analysis covers the ones
present, and `configs_present` / `configs_missing` say which. With all eight the output is unchanged.

Analysis only: no policy, no raw file and no run setting changes. Every number in
docs/results/agentdojo/agentdojo_<date>.md is read from OUT.json. Definitions: see summarise_m.py and
docs/09_AgentDojo_Plan.md (hijack: section 6.1). Errored episodes are not rerun (pre-registered).
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .hijack import STATE_CHANGING
from .summarise_g import rate
from .summarise_m import summarise_config

CONFIGS = ["M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8"]
PAIRS = {"none": ("M1", "M2"), "cene": ("M3", "M4"), "spotlighting": ("M5", "M6"), "pi_detector": ("M7", "M8")}
OMITTED = "Data omitted because a prompt injection was detected"
# Tools whose output can carry an AgentDojo banking injection vector (file contents; transaction subjects).
VECTOR_TOOLS = {"read_file", "get_most_recent_transactions"}


def _num(tid: str) -> int:
    return int(tid.rsplit("_", 1)[1])


def _cene_calls(e):
    return [c for t in (e.get("cene") or {}).get("tokens", []) for c in t["calls"]]


def _outcome(c):
    if c["decision"] == "allowed":
        return "allowed"
    if c["decision"] == "escrow":
        r = c.get("reason")
        return "held:always_escrow" if r == "tier2" else f"held:{r}"   # tier2 = contract always_escrow (Tier-1 token)
    if c["decision"] == "denied":
        return f"denied:{c.get('code')}"
    return c["decision"]


def _matches(call, hits):
    for h in hits or []:
        if h["function"] == call["tool"] and all(call["args"].get(k) == v for k, v in h["args"].items()
                                                 if v is not None):
            return True
    return False


def detector(eps: dict) -> dict:
    tool_results = flagged = 0
    by_tool, flagged_by_tool = Counter(), Counter()
    eps_flagged = 0
    for e in eps.values():
        f = 0
        for m in e["messages"]:
            if m["role"] != "tool":
                continue
            name = (m.get("tool_call") or {}).get("function", "?")
            tool_results += 1
            by_tool[name] += 1
            if OMITTED in json.dumps(m.get("content")):
                flagged += 1
                f += 1
                flagged_by_tool[name] += 1
        eps_flagged += f > 0
    certain_fp = sum(v for k, v in flagged_by_tool.items() if k not in VECTOR_TOOLS)
    return {"tool_results": tool_results, "replaced_by_detector": rate(flagged, tool_results),
            "episodes_with_a_replaced_result": rate(eps_flagged, len(eps)),
            "tool_results_by_tool": dict(by_tool), "replaced_by_tool": dict(flagged_by_tool),
            "replaced_on_tools_that_cannot_carry_an_injection": certain_fp}


def cene_detail(eps: dict) -> dict:
    legit, attacker, attacker_allowed = Counter(), Counter(), 0
    per_task = defaultdict(list)
    for k, e in eps.items():
        hits = e.get("hijack_hits") or []
        for c in _cene_calls(e):
            if not c["write"]:
                continue
            is_att = bool(e["injection_task"]) and _matches(c, hits)
            o = _outcome(c)
            if is_att:
                attacker[o] += 1
                attacker_allowed += o == "allowed"
            else:
                legit[o] += 1
                if o != "allowed":
                    per_task[e["user_task"]].append({"episode": k, "tool": c["tool"], "outcome": o})
    return {"legitimate_writes_by_outcome": dict(legit), "attacker_writes_by_outcome": dict(attacker),
            "attacker_writes_allowed": attacker_allowed,
            "legitimate_writes_not_allowed_by_user_task": {t: v for t, v in sorted(per_task.items(),
                                                                                  key=lambda kv: _num(kv[0]))}}


def successes(eps: dict) -> list:
    out = []
    for k, e in eps.items():
        if e["injection_task"] and e["security"]:
            calls = [{"function": t["function"], "args": t["args"]} for t in e["trace"]
                     if t["function"] in STATE_CHANGING]
            out.append({"pair": k, "user_task": e["user_task"], "injection_task": e["injection_task"],
                        "hijacked": e.get("hijacked"), "state_changing_calls": calls,
                        "cene_outcomes": [_outcome(c) for c in _cene_calls(e) if c["write"]] or None})
    return sorted(out, key=lambda r: (_num(r["user_task"]), _num(r["injection_task"])))


def analyse(raw_dir: str) -> dict:
    raw = Path(raw_dir)
    present = [c for c in CONFIGS if (raw / f"agentdojo_{c}.json").exists()]
    docs = {c: json.loads((raw / f"agentdojo_{c}.json").read_text()) for c in present}
    eps = {c: d["episodes"] for c, d in docs.items()}
    out = {"source_dir": str(raw).replace("\\", "/"), "configs": {}}
    if present != CONFIGS:
        out["configs_present"] = present
        out["configs_missing"] = [c for c in CONFIGS if c not in present]
    for c in present:
        out["configs"][c] = {"source": f"{out['source_dir']}/agentdojo_{c}.json", **summarise_config(c, docs[c])}
        out["configs"][c]["gpu_hours"] = round(sum(e["seconds"] for e in eps[c].values()) / 3600, 2)
    # utility per user task: no-attack configs (0/1) and attack configs (k of 9 injection tasks)
    users = sorted({e["user_task"] for e in eps["M1"].values()}, key=_num)
    out["utility_by_user_task"] = {
        u: {c: (sum(bool(e["utility"]) for e in eps[c].values() if e["user_task"] == u)) for c in present}
        for u in users}
    out["utility_by_user_task_note"] = "M1/M3/M5/M7: 1 = completed (1 episode); M2/M4/M6/M8: completions out of 9 pairs"
    # utility lost under attack, and pairs completed without defence but not with it
    for name, (na, att) in PAIRS.items():
        if att not in eps:
            continue
        lost = [k.split("|", 1)[1] for k, e in eps[att].items()
                if not e["utility"] and eps["M2"].get(k.replace(att, "M2", 1), {}).get("utility")]
        out.setdefault("utility_vs_M2", {})[name] = {
            "pairs_completed_in_M2_but_not_here": len(lost) if att != "M2" else 0}
    out["pi_detector"] = {"M7_no_attack": detector(eps["M7"]), "M8_attack": detector(eps["M8"]),
                          "note": "M7 has no attack, so every replaced tool result in M7 is a false positive"}
    out["cene"] = {"M3": cene_detail(eps["M3"]), "M4": cene_detail(eps["M4"])}
    m4_lost = []
    for k, e in eps["M4"].items():
        k2 = k.replace("M4", "M2", 1)
        if e["utility"] or not eps["M2"].get(k2, {}).get("utility"):
            continue
        legit_blocked = [_outcome(c) for c in _cene_calls(e) if c["write"] and c["decision"] != "allowed"
                         and not _matches(c, e.get("hijack_hits"))]
        m4_lost.append({"pair": k, "error": bool(e["error"]), "legitimate_write_held_or_denied": legit_blocked})
    out["cene"]["M4_pairs_completed_in_M2_but_not_M4"] = {
        "count": len(m4_lost), "with_a_legitimate_write_held_or_denied": sum(bool(r["legitimate_write_held_or_denied"])
                                                                            for r in m4_lost),
        "errored": sum(r["error"] for r in m4_lost), "pairs": m4_lost}
    out["cene"]["M4_pairs_completed_in_M2_but_not_M4"]["by_user_task"] = dict(sorted(
        Counter(r["pair"].split("|")[1] for r in m4_lost).items(), key=lambda kv: _num(kv[0])))
    # every M4 write CENE held or denied that does not match the attacker's goal (plan 6.1 signatures)
    out["cene"]["M4_non_attacker_writes_not_allowed"] = [
        {"pair": k, "tool": c["tool"], "outcome": _outcome(c), "detail": (c.get("code") or c.get("reason")),
         "args": {a: v for a, v in c["args"].items() if v is not None}}
        for k, e in sorted(eps["M4"].items()) for c in _cene_calls(e)
        if c["write"] and c["decision"] != "allowed" and not _matches(c, e.get("hijack_hits"))]
    # how CENE stopped the attacker's writes, per hijacked M4 pair (held only / denied only / both)
    stop = Counter()
    for k, e in eps["M4"].items():
        if not e["injection_task"]:
            continue
        att = [_outcome(c) for c in _cene_calls(e) if c["write"] and _matches(c, e.get("hijack_hits"))]
        if not att:
            continue
        held, denied, allowed = (any(o.startswith(p) for o in att) for p in ("held", "denied", "allowed"))
        stop["any_attacker_write_allowed" if allowed else "held_only" if held and not denied else
             "denied_only" if denied and not held else "held_and_denied"] += 1
    n_att = sum(stop.values())
    out["cene"]["M4_pairs_with_an_attacker_write"] = {
        "pairs": n_att, **{k: rate(v, n_att) for k, v in sorted(stop.items())}}
    # secondary, post-hoc: utility on the user tasks whose AgentDojo check does NOT pass with no action (G1)
    g1 = json.loads(Path("docs/results/agentdojo/g1_g2.json").read_text())
    trivial = set(g1["G1"]["tasks_whose_utility_check_passes_with_no_action"])
    out["utility_on_non_trivial_tasks"] = {
        "trivial_tasks_excluded": sorted(trivial, key=_num),
        "source_of_trivial_list": "docs/results/agentdojo/g1_g2.json : G1.tasks_whose_utility_check_passes_with_no_action",
        **{c: rate(sum(bool(e["utility"]) for e in eps[c].values() if e["user_task"] not in trivial),
                   sum(e["user_task"] not in trivial for e in eps[c].values())) for c in present},
        **{f"{c}_excluding_errors": rate(sum(bool(e["utility"]) for e in eps[c].values()
                                             if e["user_task"] not in trivial and not e["error"]),
                                         sum(e["user_task"] not in trivial and not e["error"] for e in eps[c].values()))
           for c in present}}
    out["attack_successes"] = {c: successes(eps[c]) for c in ("M4", "M6", "M8") if c in eps}
    out["attack_successes_by_injection_task"] = {c: out["configs"][c]["by_injection_task"]
                                                 for c in ("M2", "M4", "M6", "M8") if c in eps}
    out["errors"] = {c: [k for k, e in eps[c].items() if e["error"]] for c in present}
    return out


def main(raw_dir: str, out_path: str) -> int:
    out = analyse(raw_dir)
    Path(out_path).write_text(json.dumps(out, indent=1, default=str))
    for c in out["configs"]:
        s = out["configs"][c]
        line = f"{c}: utility {s['utility']['k']}/{s['utility']['n']} (excl. err {s['utility_excluding_errors']['k']}/{s['utility_excluding_errors']['n']})"
        if s["attack"]:
            line += (f" ASR {s['asr_excluding_errors']['k']}/{s['asr_excluding_errors']['n']}"
                     f" conv {s['asr_agentdojo_convention']['k']} hijack {s['hijack_rate']['k']}")
        print(line, "errors", s["errors"])
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
