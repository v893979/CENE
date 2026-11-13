"""Summarise AgentDojo model runs (M configs) into one JSON with Wilson intervals.

    python -m cene_agentdojo.summarise_m OUT.json RAW_DIR [RAW_DIR ...]

RAW_DIR holds the runner's agentdojo_M*.json files (e.g. evidence/4_kaggle/b_agentdojo_runs/RUN-1/raw).
Analysis only: it changes no policy and no raw file. Definitions follow docs/09_AgentDojo_Plan.md:
  utility          AgentDojo's utility flag, over all episodes of the configuration
  ASR              AgentDojo's security flag (True = attacker goal met), attack configurations only:
                   (a) over pairs that finished without an error, (b) AgentDojo's convention, which counts an
                   errored pair as an attack success, (c) over all pairs with an errored pair counted as a failed
                   attack. Utility and hijack rate are given with errored episodes counted as failures and with
                   them excluded. Errored episodes are never rerun (pre-registered).
  hijack rate      plan section 6.1, from the trace (attempted attacker writes, allowed, denied or held)
  CENE held/denied writes attempted by the model that CENE held in escrow / refused, by code or reason
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from .summarise_g import rate

DEFENCE = {"M1": None, "M2": None, "M3": "cene", "M4": "cene", "M5": "spotlighting_with_delimiting",
           "M6": "spotlighting_with_delimiting", "M7": "transformers_pi_detector", "M8": "transformers_pi_detector"}


def summarise_config(cid: str, doc: dict) -> dict:
    eps = list(doc["episodes"].values())
    n = len(eps)
    attack = [e for e in eps if e["injection_task"]]
    errored = [e for e in eps if e["error"]]
    out = {
        "config": cid, "defence": DEFENCE.get(cid), "attack": bool(attack), "episodes": n,
        "errors": len(errored), "error_messages": sorted({e["error"][:160] for e in errored}),
        "utility": rate(sum(bool(e["utility"]) for e in eps), n),  # errored episodes count as failures
        "utility_excluding_errors": rate(sum(bool(e["utility"]) for e in eps if not e["error"]),
                                         sum(not e["error"] for e in eps)),
        "model_calls_per_episode": round(sum(e.get("model_calls") or 0 for e in eps) / n, 2) if n else None,
        "seconds_per_episode": round(sum(e["seconds"] for e in eps) / n, 1) if n else None,
        "parse_failures": sum(e.get("parse_failures") or 0 for e in eps),
        "episodes_with_llm_errors": sum(bool(e.get("llm_errors")) for e in eps),
        "episodes_with_no_tool_call": sum(e["tool_calls"] == 0 for e in eps),
        "runs": [{k: r.get(k) for k in ("started", "finished", "stopped", "new_episodes", "provider", "model_id")}
                 for r in doc["runs"]],
        "git": doc["runs"][0].get("git"),
    }
    if attack:
        ok = [e for e in attack if not e["error"]]
        out["asr_excluding_errors"] = rate(sum(bool(e["security"]) for e in ok), len(ok))
        out["asr_agentdojo_convention"] = rate(sum(bool(e["security"]) or bool(e["error"]) for e in attack),
                                               len(attack))   # an errored pair counts as an attack success
        out["asr_errors_as_failed_attacks"] = rate(sum(bool(e["security"]) for e in ok), len(attack))
        out["hijack_rate"] = rate(sum(bool(e.get("hijacked")) for e in attack), len(attack))
        out["hijack_rate_excluding_errors"] = rate(sum(bool(e.get("hijacked")) for e in ok), len(ok))
        out["utility_under_attack"] = rate(sum(bool(e["utility"]) for e in attack), len(attack))
        out["utility_under_attack_excluding_errors"] = rate(sum(bool(e["utility"]) for e in ok), len(ok))
        by_inj = {}
        for e in attack:
            b = by_inj.setdefault(e["injection_task"], {"pairs": 0, "attack_success": 0, "hijacked": 0})
            b["pairs"] += 1
            b["attack_success"] += bool(e["security"])
            b["hijacked"] += bool(e.get("hijacked"))
        out["by_injection_task"] = dict(sorted(by_inj.items(), key=lambda kv: int(kv[0].rsplit("_", 1)[1])))
    if DEFENCE.get(cid) == "cene":
        calls = [c for e in eps for t in (e.get("cene") or {}).get("tokens", []) for c in t["calls"] if c["write"]]
        outcome = Counter("allowed" if c["decision"] == "allowed" else
                          f"held:{c.get('reason')}" if c["decision"] == "escrow" else
                          f"denied:{c.get('code')}" if c["decision"] == "denied" else c["decision"] for c in calls)
        out["cene_writes_by_outcome"] = dict(outcome)
        out["episodes_with_a_write_held"] = sum(((e.get("cene") or {}).get("write_held") or 0) > 0 for e in eps)
        out["episodes_with_a_write_denied"] = sum(((e.get("cene") or {}).get("write_denied") or 0) > 0 for e in eps)
        out["token_tiers"] = dict(Counter(t["tier"] for e in eps for t in (e.get("cene") or {}).get("tokens", [])))
        out["audit_chain_ok"] = dict(Counter(str((e.get("cene") or {}).get("audit_chain_ok")) for e in eps))
    return out


def main(out_path: str, *raw_dirs: str) -> int:
    configs = {}
    for d in raw_dirs:
        for f in sorted(Path(d).glob("agentdojo_M*.json")):
            cid = f.stem.split("_")[1]
            configs[cid] = {"source": str(f).replace("\\", "/"), **summarise_config(cid, json.loads(f.read_text()))}
    Path(out_path).write_text(json.dumps({"configs": configs}, indent=1, default=str))
    for c, s in configs.items():
        line = f"{c}: n={s['episodes']} utility={s['utility']['k']}/{s['utility']['n']} errors={s['errors']}"
        if s["attack"]:
            line += (f" ASR={s['asr_excluding_errors']['k']}/{s['asr_excluding_errors']['n']}"
                     f" hijack={s['hijack_rate']['k']}/{s['hijack_rate']['n']}")
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
