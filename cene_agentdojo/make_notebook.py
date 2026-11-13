"""Generate notebooks/agentdojo_cene.ipynb (run setup r4-model2).   python -m cene_agentdojo.make_notebook"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "notebooks" / "agentdojo_cene.ipynb"
CELLS: list[tuple[str, str]] = []


def md(s):
    CELLS.append(("markdown", s))


def code(s):
    CELLS.append(("code", s))


md(r"""
# CENE on AgentDojo (banking) — Kaggle run, run setup r4-model2 (Qwen3-8B)

Policy: tag `agentdojo-policy-v1` (unchanged). Run setup: tag `agentdojo-run-r4-model2` (`cene_agentdojo/run_config.yaml`).
Model: Qwen3-8B (Kaggle Models `qwen-lm/qwen-3/transformers/8b`). Outputs dataset: `cene-agentdojo-outputs-model2` only;
the notebook stops if any attached result or CHECK file comes from another model.
Steps for you: `docs/10_AgentDojo_Kaggle_Steps.md`.

* `SESSION = "CHECK"` (under 40 min): start the model server, one chat request, then the tool-calling mode
  selection on the 3 smoke tasks under M1 (native first). Writes `check.json` (per-episode tool calls, parse
  failures, model calls, seconds, utility, and the quota-week plan) and `tool_mode.json`.
* `SESSION = "RUN"`: smoke (first RUN only), then this week's configurations from `check.json`, resuming from
  earlier outputs; no new episode starts after min(11 h, quota left - 1.5 h).
""")

code(r"""
# ---- the only cell you edit ---------------------------------------------------------------
SESSION = "CHECK"        # "CHECK" first; then "RUN"
WEEK = 1                 # RUN only: 1 = this quota week, 2 = after the reset
QUOTA_LEFT_HOURS = None  # RUN only: GPU hours Kaggle shows as left this week, e.g. 13.2 (required)
SERVING_ID = None        # leave None (run_config serving.start_option = hf-qwen3-8b)
""")

code(r"""
import glob, hashlib, json, os, shutil, subprocess, sys, time, zipfile, pathlib, re
T0 = time.time()
W = pathlib.Path("/kaggle/working")
REPO = W / "repo"
rev = glob.glob("/kaggle/input/**/REVISION.json", recursive=True)
if not REPO.exists():
    if rev:
        shutil.copytree(pathlib.Path(rev[0]).parent, REPO)
    else:
        z = glob.glob("/kaggle/input/**/cene-agentdojo-*.zip", recursive=True)
        assert z, "attach the Kaggle Dataset made by cene_agentdojo/package_kaggle.sh"
        zipfile.ZipFile(z[0]).extractall(REPO)
REVISION = json.loads((REPO / "REVISION.json").read_text())
assert REVISION.get("policy_tag") == "agentdojo-policy-v1" and REVISION.get("run_tag") == "agentdojo-run-r4-model2", REVISION
REPO_INPUT = pathlib.Path(rev[0]).parent if rev else None
print("repo", REPO, REVISION)
sys.path.insert(0, str(REPO))
import yaml
CFG = yaml.safe_load((REPO / "cene_agentdojo/run_config.yaml").read_text())
assert CFG.get("run_setup") == "r4-model2"
assert REVISION.get("model") == CFG["model2"]["chosen"], (REVISION.get("model"), CFG["model2"]["chosen"])
if SESSION == "RUN":
    assert QUOTA_LEFT_HOURS is not None, "set QUOTA_LEFT_HOURS to the GPU hours Kaggle shows as left this week"
# Earlier sessions' outputs (attached as a dataset) -> /kaggle/working, so the runner resumes.
RESUMED = []
for p in glob.glob("/kaggle/input/**/*.json", recursive=True):
    p = pathlib.Path(p)
    if REPO_INPUT and REPO_INPUT in p.parents:
        continue
    if re.fullmatch(r"(agentdojo_M\d|smoke|tool_mode|check|select_.*|environment_.*|status)\.json", p.name) \
            and not (W / p.name).exists():
        shutil.copy(p, W / p.name); RESUMED.append(p.name)
print("resumed from earlier outputs:", RESUMED)
""")

code(r"""
# AgentDojo pinned and hash-checked; transformers pinned (run_config serving.transformers_version).
ad = CFG["agentdojo"]
subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "-q", "-d", "/tmp/whl", ad["package"]], check=True)
whl = glob.glob("/tmp/whl/agentdojo-*.whl")[0]
sha = hashlib.sha256(open(whl, "rb").read()).hexdigest()
assert sha == ad["wheel_sha256"], (sha, ad["wheel_sha256"])
subprocess.run([sys.executable, "-m", "pip", "install", "-q", whl], check=True)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(REPO / "requirements.txt")], check=False)

def pkg_version(mod):
    r = subprocess.run([sys.executable, "-c", f"import {mod}; print({mod}.__version__)"], capture_output=True, text=True)
    return r.stdout.strip() or None

gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                     capture_output=True, text=True).stdout.strip()
print("gpus:", gpu)
""")

code(r"""
# Models: matched by an exact path component equal to the option's folder name.
def find_model(folder, must_contain=None):
    hits = sorted({str(pathlib.Path(p).parent) for p in glob.glob("/kaggle/input/**/config.json", recursive=True)
                   if folder in pathlib.Path(p).parts and (must_contain is None or must_contain in pathlib.Path(p).parts)})
    return hits[0] if hits else None

def model_listing(path):
    return sorted((p.name, p.stat().st_size) for p in pathlib.Path(path).iterdir() if p.is_file())

for o in CFG["serving"]["order"]:
    print(o["id"], o["folder"], "->", find_model(o["folder"], o.get("path_must_contain")))
""")

code(r"""
# Serving, in the pre-registered order, starting at SERVING_ID (default serving.start_option).
import requests
PORT = 8000
SV = CFG["serving"]
LOG = open(W / "server.log", "a")

def stop_server():
    subprocess.run(["pkill", "-f", "vllm serve"], check=False)
    subprocess.run(["pkill", "-f", "cene_agentdojo.serve_hf"], check=False)
    time.sleep(8)

def wait_ready(proc, minutes):
    t = time.time()
    while time.time() - t < minutes * 60:
        if proc.poll() is not None:
            return False
        try:
            if requests.get(f"http://localhost:{PORT}/v1/models", timeout=5).ok:
                return True
        except Exception:
            pass
        time.sleep(10)
    proc.kill(); return False

def first_request(name):
    try:
        r = requests.post(f"http://localhost:{PORT}/v1/chat/completions", timeout=600,
                          json={"model": name, **SV["first_request"], "max_tokens": 16})
        txt = r.json()["choices"][0]["message"]["content"] if r.ok else None
        return bool(txt and txt.strip()), (txt if r.ok else r.text[:500])
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def start(opt, path, extra=()):
    stop_server()
    env = dict(os.environ)
    if opt["stack"] == "vllm":
        if not pkg_version("vllm"):
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "vllm"], check=False)
        helptext = (subprocess.run(["vllm", "serve", "--help=all"], capture_output=True, text=True).stdout or
                    subprocess.run(["vllm", "serve", "--help"], capture_output=True, text=True).stdout)
        missing = [w for w in opt["only_if_help_mentions"] if w not in helptext]
        if missing:
            return None, {"skipped": f"vllm serve --help does not mention {missing}", "vllm": pkg_version("vllm")}
        args = ["vllm", "serve", path, "--port", str(PORT), "--served-model-name", opt["folder"], *opt["args"], *extra]
        if opt.get("max_new_tokens_flag") and opt["max_new_tokens_flag"] in helptext:
            args += [opt["max_new_tokens_flag"], json.dumps({"max_new_tokens": SV["max_new_tokens"]})]
        meta = {"vllm": pkg_version("vllm")}
    else:
        if pkg_version("transformers") != SV["transformers_version"]:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", f"transformers=={SV['transformers_version']}",
                            "accelerate"], check=False)
        env["CUDA_VISIBLE_DEVICES"] = opt["gpus"]
        env["PYTHONPATH"] = str(REPO)
        args = [sys.executable, "-m", "cene_agentdojo.serve_hf", "--model", path, "--name", opt.get("name", opt["folder"]),
                "--port", str(PORT), "--dtype", opt["dtype"], "--attn-implementation", opt["attn_implementation"],
                "--max-memory-gib", str(opt["max_memory_gib_per_gpu"]), "--max-new-tokens", str(SV["max_new_tokens"]),
                "--info-out", str(W / "serve_info.json")] + ([] if opt.get("text_only", True) else ["--no-text-only"]) \
            + (["--family", opt["family"]] if opt.get("family") else [])
        meta = {"transformers": pkg_version("transformers"), "torch": pkg_version("torch")}
    LOG.write("\n\n### " + " ".join(args) + "\n"); LOG.flush()
    proc = subprocess.Popen(args, stdout=LOG, stderr=subprocess.STDOUT, env=env, cwd=REPO)
    meta["args"] = args
    return (proc if wait_ready(proc, opt["timeout_minutes"]) else None), meta

ids = [o["id"] for o in SV["order"]]
first = ids.index(SERVING_ID or SV["start_option"])
order = SV["order"][first:]
SERVED, attempts = None, [{"id": i, "result": "not tried (see run_config serving.start_option)"}
                          for i in ids[:first]]
for opt in order:
    path = find_model(opt["folder"], opt.get("path_must_contain"))
    if not path:
        attempts.append({"id": opt["id"], "result": f"model folder {opt['folder']} not attached"}); continue
    t = time.time()
    proc, meta = start(opt, path)
    ok, reply = first_request(opt.get("name", opt["folder"])) if proc else (False, meta.get("skipped", "server not ready / exited"))
    attempts.append({"id": opt["id"], "path": path, **meta, "result": "ok" if ok else "failed", "detail": reply,
                     "seconds": round(time.time() - t)})
    if ok:
        info = json.loads((W / "serve_info.json").read_text()) if opt["stack"] == "transformers" and \
            (W / "serve_info.json").exists() else {}
        SERVED = {**opt, "path": path, "files": model_listing(path), "server_info": info, **meta}
        break
    stop_server()
print(json.dumps(attempts, indent=1)[:4000])
ENV = {"revision": REVISION, "session": SESSION, "week": WEEK, "quota_left_hours_at_start": QUOTA_LEFT_HOURS,
       "gpus": gpu, "agentdojo_wheel_sha256": sha, "resumed": RESUMED, "serve_attempts": attempts, "served": SERVED,
       "decoding": CFG["decoding"],
       "versions": {m: pkg_version(m) for m in ("vllm", "transformers", "torch", "agentdojo", "openai")},
       "setup_seconds": round(time.time() - T0)}
(W / f"environment_{SESSION}.json").write_text(json.dumps(ENV, indent=1, default=str))
assert SERVED, "no serving option worked: stop and report (run_config.yaml serving.failure_rule)"
SERVED_NAME = SERVED.get("name", SERVED["folder"])
# Resume guard: attached outputs must come from THIS model (never resume from another model's results).
for f in sorted(W.glob("agentdojo_M*.json")) + sorted(W.glob("select_*.json")) + sorted(W.glob("smoke.json")):
    runs = json.loads(f.read_text()).get("runs", [])
    other = {r.get("model_id") for r in runs} - {SERVED_NAME}
    assert not other, f"{f.name} comes from another model {other}: attach only cene-agentdojo-outputs-model2"
if (W / "check.json").exists():
    assert json.loads((W / "check.json").read_text()).get("served") == SERVED["id"], "check.json is from another model"
print("resume guard ok: all attached outputs are from", SERVED_NAME)
SETUP_HOURS = (time.time() - T0) / 3600
print("served:", SERVED["id"], "| setup hours:", round(SETUP_HOURS, 2))
""")

code(r"""
# Runner: AgentDojo client process with the GPUs hidden (the PI detector then uses the CPU).
S = CFG["sessions"]
MAX_H = 0.75 if SESSION == "CHECK" else min(S["session_max_hours"], QUOTA_LEFT_HOURS - S["quota_margin_hours"])
print("this session starts no new episode after", round(MAX_H, 2), "h")
def run(configs, out, mode, only=None, label=None, max_episodes=None):
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": "", "PYTHONPATH": str(REPO), "LOCAL_LLM_PORT": str(PORT)}
    remaining = MAX_H - (time.time() - T0) / 3600
    cmd = [sys.executable, "-m", "cene_agentdojo.run", *configs, "--out", str(out), "--port", str(PORT),
           "--model-id", SERVED_NAME, "--provider", mode["provider"],
           "--tool-delimiter", mode.get("tool_delimiter", "tool"), "--stop-after-hours", f"{max(0.0, remaining):.3f}"]
    if only: cmd += ["--only", *only]
    if label: cmd += ["--label", label]
    if max_episodes: cmd += ["--max-episodes", str(max_episodes)]
    print(" ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=REPO, env=env).returncode

def episodes(path):
    p = pathlib.Path(path)
    return json.loads(p.read_text())["episodes"] if p.exists() else {}

tc = CFG["tool_calling"]
def select_mode():
    # Tool-calling mode selection on the smoke tasks under M1 (run_config tool_calling.selection_rule).
    selection, chosen = [], None
    for mode in tc["candidates"]:
        if mode["provider"] == "native" and SERVED["stack"] != "transformers":
            selection.append({"mode": mode, "result": "skipped: native mode needs serve_hf"}); continue
        out = W / f"select_{mode['provider']}_{mode.get('tool_delimiter', 'tools')}.json"
        run(["M1"], out, mode, only=tc["smoke_user_tasks"], label="tool-mode-selection")
        eps = list(episodes(out).values())
        req_err = any(e.get("llm_errors") for e in eps) or any(e.get("error") for e in eps)
        with_calls = sum(e["tool_calls"] > 0 for e in eps)
        ok = (not req_err) and with_calls >= 2 and len(eps) == 3
        selection.append({"mode": mode, "episodes": len(eps), "with_tool_calls": with_calls,
                          "request_errors": req_err, "qualifies": ok, "file": out.name})
        if ok:
            chosen = mode; break
    (W / "tool_mode.json").write_text(json.dumps({"selected": chosen, "attempts": selection}, indent=1))
    print(json.dumps(selection, indent=1))
    return chosen, selection
""")

code(r"""
# CHECK: mode selection on the 3 smoke tasks, per-episode metrics, quota-week plan.
if SESSION == "CHECK":
    from cene_agentdojo.sessions import plan_weeks
    TOOL_MODE, selection = select_mode()
    tried = [a for a in selection if a.get("file")]
    rep = next((a for a in selection if a.get("qualifies")), tried[-1] if tried else None)
    eps = list(episodes(W / rep["file"]).values()) if rep else []
    per_ep = [{"user_task": e["user_task"], "utility": e["utility"], "tool_calls": e["tool_calls"],
               "parse_failures": e.get("parse_failures"), "model_calls": e.get("model_calls"),
               "seconds": e["seconds"], "seconds_per_call": e.get("llm_call_seconds"),
               "tokens_per_call": e.get("llm_call_tokens"), "llm_errors": e.get("llm_errors"), "error": e["error"],
               "thinking_stripped": [(m or {}).get("thinking_regions") for m in e.get("llm_call_meta", [])],
               "final_text": (e.get("final_text") or "")[:300]} for e in eps]
    calls = [s for e in eps for s in (e.get("llm_call_seconds") or [])]
    mean_ep = sum(e["seconds"] for e in eps) / len(eps) if eps else None
    plan = None
    if mean_ep:
        check_hours = (time.time() - T0) / 3600
        plan = plan_weeks(mean_ep * CFG["check"]["timing_margin"], week1_budget_hours=S["week1_budget_hours"],
                          week2_budget_hours=S["week2_budget_hours"], week1_used_hours=check_hours,
                          setup_hours=max(SETUP_HOURS, 0.25), session_max_hours=S["session_max_hours"],
                          drop_order=tuple(S["drop_order"]))
    CHECK = {"served": SERVED["id"], "setup_hours": round(SETUP_HOURS, 2), "selected_mode": TOOL_MODE,
             "mode_reported": rep["mode"] if rep else None, "episodes": per_ep,
             "mean_seconds_per_episode": round(mean_ep, 1) if mean_ep else None,
             "mean_seconds_per_call": round(sum(calls) / len(calls), 2) if calls else None,
             "mean_model_calls_per_episode": round(len(calls) / len(eps), 2) if eps else None,
             "timing_margin": CFG["check"]["timing_margin"], "plan": plan,
             "check_session_hours": round((time.time() - T0) / 3600, 2)}
    (W / "check.json").write_text(json.dumps(CHECK, indent=1))
    print(json.dumps({k: v for k, v in CHECK.items() if k != "episodes"}, indent=1))
    for e in per_ep:
        print(e["user_task"], "utility", e["utility"], "tool_calls", e["tool_calls"], "parse_failures",
              e["parse_failures"], "model_calls", e["model_calls"], "seconds", e["seconds"])
    assert TOOL_MODE, "no tool-calling mode qualifies: stop and report (no prompt tuning)"
""")

code(r"""
# RUN: tool mode from CHECK (tool_mode.json attached); smoke = 3 x M3, 3 x M2 pairs, 1 x M7 (once).
if SESSION == "RUN":
    TOOL_MODE = json.loads((W / "tool_mode.json").read_text())["selected"] if (W / "tool_mode.json").exists() else None
    if TOOL_MODE is None:
        TOOL_MODE, _ = select_mode()
    assert TOOL_MODE, "no tool-calling mode qualifies: stop and report (no prompt tuning)"
    print("TOOL_MODE =", TOOL_MODE)
    smoke = W / "smoke.json"
    run(["M3"], smoke, TOOL_MODE, only=tc["smoke_user_tasks"], label="smoke")
    run(["M2"], smoke, TOOL_MODE, only=[f"{u}:{i}" for u, i in tc["smoke_m2_pairs"]], label="smoke")
    run(["M7"], smoke, TOOL_MODE, only=tc["smoke_user_tasks"][:1], label="smoke")
    sel = json.loads((W / "tool_mode.json").read_text())
    q = [a for a in sel.get("attempts", []) if a.get("qualifies")]
    no_calls = 3 - (q[0]["with_tool_calls"] if q else 3)
    for k, e in episodes(smoke).items():
        print(k, "utility", e["utility"], "security", e["security"], "calls", e["tool_calls"],
              "parse_failures", e.get("parse_failures"), "err", e["error"])
    assert no_calls < 2, "2+ of 3 M1 smoke episodes had no tool call: stop and report"
""")

code(r"""
# RUN: this week's configurations from check.json's plan, resuming; then status.json.
if SESSION == "RUN":
    from cene_agentdojo.sessions import EPISODES
    plan = json.loads((W / "check.json").read_text())["plan"]
    queue = plan["week1"]["configs"] + (plan["week2"]["configs"] if WEEK >= 2 else [])
    print("week", WEEK, "queue:", queue, "| dropped by plan:", plan["dropped"])
    for c in queue:
        if (time.time() - T0) / 3600 >= MAX_H:
            break
        rc = run([c], W / f"agentdojo_{c}.json", TOOL_MODE)
        print(c, "exit", rc, "episodes", len(episodes(W / f"agentdojo_{c}.json")), "of", EPISODES[c], flush=True)
    status = {c: {"done": len(episodes(W / f"agentdojo_{c}.json")), "of": EPISODES[c]}
              for c in plan["week1"]["configs"] + plan["week2"]["configs"]}
    status["_week"] = WEEK
    status["_week_done"] = all(status[c]["done"] >= status[c]["of"] for c in queue)
    status["_all_done"] = all(v["done"] >= v["of"] for k, v in status.items() if not k.startswith("_"))
    status["_session_hours"] = round((time.time() - T0) / 3600, 2)
    (W / "status.json").write_text(json.dumps(status, indent=1))
    print(json.dumps(status, indent=1))
    ENV["finished"] = time.time(); ENV["tool_mode"] = TOOL_MODE
    (W / f"environment_{SESSION}.json").write_text(json.dumps(ENV, indent=1, default=str))
try:
    print("server stats:", requests.get(f"http://localhost:{PORT}/stats", timeout=5).json())
except Exception:
    pass
stop_server()
""")


def build() -> dict:
    cells = []
    for kind, src in CELLS:
        lines = src.strip("\n").split("\n")
        c = {"cell_type": kind, "metadata": {}, "source": [l + "\n" for l in lines[:-1]] + [lines[-1]]}
        if kind == "code":
            c.update(execution_count=None, outputs=[])
        cells.append(c)
    return {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                                        "name": "python3"}, "language_info": {"name": "python"}},
            "nbformat": 4, "nbformat_minor": 5}


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=1), encoding="utf-8")
    print("wrote", OUT, len(CELLS), "cells")
