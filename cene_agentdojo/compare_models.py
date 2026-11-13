"""Side-by-side comparison of two models' AgentDojo runs (second-model replication, plan r9-r10).

    python -m cene_agentdojo.compare_models OUT.json NAME_A=ANALYSIS_A.json NAME_B=ANALYSIS_B.json

Each ANALYSIS is the output of `python -m cene_agentdojo.analyse_m RAW_DIR ANALYSIS.json`; the raw episodes are
read from its `source_dir`. Analysis only: no policy, raw file or run setting changes, and nothing is rerun.
Every rate is k / n with a Wilson 95 % interval (summarise_g.rate), with the same three error conventions as the
single-model report (errors counted as failures, errors excluded, and AgentDojo's convention for ASR).

Pre-registered (plan r9): the side-by-side table for every configuration, Wilson intervals, whether the ordering
of defences holds for both models, and the same error reporting. The PI-detector replacement rate uses
analyse_m.detector for both models. Labelled post-hoc: the paired (same task pair) counts and exact McNemar
p-values, and the per-call output statistics.
"""
from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path

from .analyse_m import OMITTED
from .summarise_g import rate

CONFIGS = ["M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8"]
ATTACK = ["M2", "M4", "M6", "M8"]
NO_ATTACK = ["M1", "M3", "M5", "M7"]
DEFENCE_NAME = {"M1": "none", "M2": "none", "M3": "CENE", "M4": "CENE", "M5": "spotlighting", "M6": "spotlighting",
                "M7": "PI detector", "M8": "PI detector"}
MAX_NEW_TOKENS = 1024


def _eps(analysis: dict, c: str) -> dict:
    return json.loads(Path(analysis["configs"][c]["source"]).read_text())["episodes"]


def _pair(key: str) -> str:
    return key.split("|", 1)[1]


def mcnemar_exact(b: int, c: int) -> float | None:
    """Two-sided exact McNemar (binomial on the discordant pairs)."""
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    p = 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, p), 4)


def paired(ea: dict, eb: dict, field: str, ca: str, cb: str) -> dict:
    """Same pair under two settings: both, only first, only second, neither (errored pairs left out)."""
    a = {_pair(k): e for k, e in ea.items() if not e["error"]}
    b = {_pair(k): e for k, e in eb.items() if not e["error"]}
    common = sorted(set(a) & set(b))
    both = sum(bool(a[p][field]) and bool(b[p][field]) for p in common)
    only_a = sum(bool(a[p][field]) and not bool(b[p][field]) for p in common)
    only_b = sum(not bool(a[p][field]) and bool(b[p][field]) for p in common)
    return {"compared": f"{ca} vs {cb}", "field": field, "pairs": len(common), "both": both,
            f"only_{ca}": only_a, f"only_{cb}": only_b, "neither": len(common) - both - only_a - only_b,
            "mcnemar_exact_p": mcnemar_exact(only_a, only_b)}


def overlap(x: dict, y: dict) -> bool | None:
    if not x or not y or x.get("wilson95") is None or y.get("wilson95") is None:
        return None
    return x["wilson95"][0] <= y["wilson95"][1] and y["wilson95"][0] <= x["wilson95"][1]


ROW_FIELDS = ["utility", "utility_excluding_errors", "asr_excluding_errors", "asr_agentdojo_convention",
              "asr_errors_as_failed_attacks", "hijack_rate", "hijack_rate_excluding_errors"]


def output_stats(eps: dict) -> dict:
    meta = [m for e in eps.values() for m in (e.get("llm_call_meta") or [])]
    toks = [t for e in eps.values() for t in (e.get("llm_call_tokens") or [])]
    return {"model_calls": len(meta),
            "thinking_regions_stripped": sum(m.get("thinking_regions") or 0 for m in meta),
            "reasoning_chars_stripped": sum(m.get("reasoning_chars") or 0 for m in meta),
            "parse_failures": sum(m.get("parse_failures") or 0 for m in meta),
            "calls_reaching_max_new_tokens": sum(1 for t in toks if t and t[1] >= MAX_NEW_TOKENS),
            "mean_prompt_tokens": round(sum(t[0] for t in toks) / len(toks), 1) if toks else None,
            "mean_completion_tokens": round(sum(t[1] for t in toks) / len(toks), 1) if toks else None}


def detector_by_tool(d: dict) -> dict:
    return {t: rate(d["replaced_by_tool"].get(t, 0), n) for t, n in sorted(d["tool_results_by_tool"].items())}


def split_non_attacker(analysis: dict, eps_m4: dict) -> dict:
    """Post-hoc: M4 writes CENE held or denied that do not match the injection task's ground-truth signature,
    split by whether the recipient starts with the same 10 characters as the signature's recipient (the attacker
    IBAN), i.e. attacker-directed writes with a mistyped IBAN, versus writes to other recipients."""
    out = {}
    for r in analysis["cene"]["M4_non_attacker_writes_not_allowed"]:
        sigs = eps_m4[r["pair"]].get("hijack_signatures") or []
        att = {s["args"].get("recipient")[:10] for s in sigs if s["args"].get("recipient")}
        rec = r["args"].get("recipient")
        kind = "no_recipient_arg" if not rec else ("recipient_like_attacker_iban" if rec[:10] in att else
                                                   "other_recipient")
        b = out.setdefault(r["outcome"], {}).setdefault(kind, {"writes": 0, "by_user_task": {}})
        b["writes"] += 1
        u = r["pair"].split("|")[1]
        b["by_user_task"][u] = b["by_user_task"].get(u, 0) + 1
    return out


def ordering(analysis: dict, configs: list[str], field: str) -> list[dict]:
    rows = [{"config": c, "defence": DEFENCE_NAME[c], **analysis["configs"][c][field]} for c in configs]
    return sorted(rows, key=lambda r: (r["rate"], r["config"]))


def compare(named: dict[str, dict]) -> dict:
    (na, a), (nb, b) = named.items()
    both = [c for c in CONFIGS if c in a["configs"] and c in b["configs"]]
    out = {"models": {n: {"analysis": NAMED_PATHS.get(n), "source_dir": d["source_dir"],
                          "configs": list(d["configs"]), "configs_missing": d.get("configs_missing", [])}
                      for n, d in named.items()},
           "configs_in_both": both,
           "configs_pending": sorted(set(CONFIGS) - set(both), key=CONFIGS.index)}
    eps = {n: {c: _eps(d, c) for c in d["configs"]} for n, d in named.items()}
    # 1. side-by-side table, every configuration
    table = {}
    for c in CONFIGS:
        row = {"defence": DEFENCE_NAME[c], "attack": c in ATTACK}
        for n, d in named.items():
            s = d["configs"].get(c)
            row[n] = None if s is None else {
                **{f: s.get(f) for f in ROW_FIELDS if f in s}, "episodes": s["episodes"], "errors": s["errors"],
                "error_messages": s["error_messages"], "gpu_hours": s.get("gpu_hours"),
                "seconds_per_episode": s["seconds_per_episode"], "model_calls_per_episode": s["model_calls_per_episode"],
                "episodes_with_no_tool_call": s["episodes_with_no_tool_call"]}
        if c in both:
            row["intervals_overlap"] = {f: overlap(row[na].get(f), row[nb].get(f)) for f in ROW_FIELDS
                                        if f in row[na]}
            row["difference_" + nb + "_minus_" + na] = {
                f: round(row[nb][f]["rate"] - row[na][f]["rate"], 4) for f in ROW_FIELDS if f in row[na]}
            row["paired_between_models"] = {"utility": paired(eps[na][c], eps[nb][c], "utility", na, nb)}
            if c in ATTACK:
                row["paired_between_models"]["attack_success"] = paired(eps[na][c], eps[nb][c], "security", na, nb)
        table[c] = row
    out["table"] = table
    # 2. ordering of defences, per model, over the configurations both models have
    att = [c for c in ATTACK if c in both]
    noatt = [c for c in NO_ATTACK if c in both]
    order = {}
    for n, d in named.items():
        order[n] = {
            "asr_low_to_high": ordering(d, att, "asr_excluding_errors"),
            "utility_under_attack_low_to_high": ordering(d, att, "utility"),
            "utility_no_attack_low_to_high": ordering(d, noatt, "utility"),
            "paired_within_model": {
                "attack_success": [paired(eps[n][x], eps[n][y], "security", x, y)
                                   for i, x in enumerate(att) for y in att[i + 1:]],
                "utility_under_attack": [paired(eps[n][x], eps[n][y], "utility", x, y)
                                         for i, x in enumerate(att) for y in att[i + 1:]]}}
    seq = {n: [r["config"] for r in order[n]["asr_low_to_high"]] for n in named}
    out["ordering_of_defences"] = {
        **order, "asr_order_same_for_both": seq[na] == seq[nb], "asr_order": seq,
        "utility_under_attack_order": {n: [r["config"] for r in order[n]["utility_under_attack_low_to_high"]]
                                       for n in named},
        "note": "Ordered by rate, errors excluded for ASR and counted as failures for utility; ties by config id. "
                "Configurations pending for either model are left out (configs_pending)."}
    # 3. PI detector replacement rate, same method (analyse_m.detector) for both models
    out["pi_detector"] = {n: {k: {**d["pi_detector"][k], "replaced_rate_by_tool": detector_by_tool(d["pi_detector"][k])}
                              for k in ("M7_no_attack", "M8_attack")}
                          for n, d in named.items() if "pi_detector" in d}
    out["pi_detector_attack_successes_post_hoc"] = {n: [
        {"pair": _pair(k), "injected_vectors": e["injected_vectors"],
         "tool_results_replaced": {t: [OMITTED in json.dumps(m.get("content")) for m in e["messages"]
                                       if m["role"] == "tool" and (m.get("tool_call") or {}).get("function") == t]
                                   for t in sorted({(m.get("tool_call") or {}).get("function") for m in e["messages"]
                                                    if m["role"] == "tool"} - {None})}}
        for k, e in sorted(eps[n]["M8"].items()) if e["security"]] for n in named if "M8" in eps[n]}
    out["pi_detector"]["method"] = ("analyse_m.detector: share of tool-result messages whose content contains "
                                    "AgentDojo's replacement text 'Data omitted because a prompt injection was "
                                    "detected'; in M7 (no attack) every replacement is a false positive")
    # 4. CENE detail
    out["cene"] = {n: {"M3": d["cene"]["M3"], "M4": d["cene"]["M4"],
                       "M4_pairs_with_an_attacker_write": d["cene"]["M4_pairs_with_an_attacker_write"],
                       "M3_writes_by_outcome": d["configs"]["M3"].get("cene_writes_by_outcome"),
                       "M4_writes_by_outcome": d["configs"]["M4"].get("cene_writes_by_outcome"),
                       "M3_episodes_with_a_write_held": d["configs"]["M3"].get("episodes_with_a_write_held"),
                       "M3_episodes_with_a_write_denied": d["configs"]["M3"].get("episodes_with_a_write_denied"),
                       "M4_pairs_completed_in_M2_but_not_M4": {
                           k: v for k, v in d["cene"]["M4_pairs_completed_in_M2_but_not_M4"].items() if k != "pairs"},
                       "audit_chain_ok": {c: d["configs"][c].get("audit_chain_ok") for c in ("M3", "M4")},
                       "M4_non_attacker_writes_not_allowed_split_post_hoc": split_non_attacker(d, eps[n]["M4"])}
                   for n, d in named.items()}
    # 5. errors, the other views, and per-call output statistics (post-hoc)
    out["errors"] = {n: d["errors"] for n, d in named.items()}
    out["utility_on_non_trivial_tasks"] = {n: d["utility_on_non_trivial_tasks"] for n, d in named.items()}
    out["attack_successes_by_injection_task"] = {n: d["attack_successes_by_injection_task"] for n, d in named.items()}
    out["utility_by_user_task"] = {n: d["utility_by_user_task"] for n, d in named.items()}
    out["output_stats_post_hoc"] = {n: {c: output_stats(eps[n][c]) for c in named[n]["configs"]} for n in named}
    return out


NAMED_PATHS: dict[str, str] = {}


def main(out_path: str, *specs: str) -> int:
    named = {}
    for s in specs:
        n, p = s.split("=", 1)
        NAMED_PATHS[n] = p.replace("\\", "/")
        named[n] = json.loads(Path(p).read_text())
    out = compare(named)
    Path(out_path).write_text(json.dumps(out, indent=1, default=str))
    na, nb = named
    for c, r in out["table"].items():
        cells = []
        for n in (na, nb):
            v = r[n]
            if v is None:
                cells.append(f"{n}: pending")
                continue
            t = f"{n}: U {v['utility']['k']}/{v['utility']['n']}"
            if r["attack"]:
                t += f" ASR {v['asr_excluding_errors']['k']}/{v['asr_excluding_errors']['n']}"
            cells.append(t)
        print(c, " | ".join(cells))
    print("ASR order:", out["ordering_of_defences"]["asr_order"], "same:",
          out["ordering_of_defences"]["asr_order_same_for_both"])
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
