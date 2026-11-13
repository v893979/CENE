"""Run the AgentDojo banking matrix (G1, G2 locally; M1-M8 against a vLLM server).

    python -m cene_agentdojo.run G1 G2 --out docs/results/agentdojo/g1_g2.json
    python -m cene_agentdojo.run M1 --out /kaggle/working/agentdojo_M1.json --provider local --tool-delimiter user
    python -m cene_agentdojo.run M2 --out ... --only user_task_0:injection_task_0     # smoke subsets

Every episode is written as soon as it finishes; rerunning with the same --out resumes (completed episodes
are skipped, the restart is recorded). Nothing is dropped: an episode that raises is recorded with its error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import random
import subprocess
import sys
import time
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).parent / "run_config.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _num(task_id: str) -> int:
    return int(task_id.rsplit("_", 1)[1])


def episode_index(user_task_id: str, injection_task_id: str | None) -> int:
    return 100 * _num(user_task_id) + (99 if injection_task_id is None else _num(injection_task_id))


def git_info() -> dict:
    def g(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return None
    info = {"commit": g("rev-parse", "HEAD"), "describe": g("describe", "--tags", "--always", "--dirty")}
    rev = ROOT / "REVISION.json"  # written by `git archive` packaging for the Kaggle Dataset
    if not info["commit"] and rev.exists():
        info.update(json.loads(rev.read_text()))
    return info


# ------------------------------------------------------------------ pipelines
class Recorder:
    """Pass-through element at the end of the pipeline that keeps the final messages."""

    name = None

    def __init__(self):
        self.messages = []

    def query(self, query, runtime, env, messages=[], extra_args={}):
        self.messages = list(messages)
        return query, runtime, env, messages, extra_args


class CountingClient:
    """Wraps the OpenAI client to count exceptions LocalLLM would otherwise swallow, and to time each call."""

    def __init__(self, client):
        self._client = client
        self.errors: list[str] = []
        self.seconds: list[float] = []
        self.tokens: list[list] = []
        self.meta: list[dict | None] = []   # serve_hf's x_cene per call (parse failures, thinking), if present
        outer = self

        class _Completions:
            def create(self, **kw):
                t0 = time.time()
                try:
                    r = client.chat.completions.create(**kw)
                    u = getattr(r, "usage", None)
                    outer.tokens.append([getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None)])
                    x = (getattr(r, "model_extra", None) or {}).get("x_cene")
                    outer.meta.append({k: v for k, v in x.items() if k != "raw"} | {"raw": (x.get("raw") or "")[:600]}
                                      if isinstance(x, dict) else None)
                    return r
                except Exception as e:
                    outer.errors.append(f"{type(e).__name__}: {str(e)[:300]}")
                    raise
                finally:
                    outer.seconds.append(round(time.time() - t0, 3))

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()

    def __getattr__(self, name):
        return getattr(self._client, name)


def make_llm(provider: str, model_id: str, tool_delimiter: str, cfg: dict, port: int):
    import openai
    from agentdojo.agent_pipeline.llms.local_llm import LocalLLM
    from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM

    client = CountingClient(openai.OpenAI(api_key="EMPTY", base_url=f"http://localhost:{port}/v1"))
    d = cfg["decoding"]
    if provider == "native":  # r3: AgentDojo's OpenAI-compatible element (as vllm_parsed) against serve_hf + tools
        llm = OpenAILLM(client, model_id, temperature=d["temperature"])
    elif provider == "local":
        llm = LocalLLM(client, model_id, temperature=d["temperature"], top_p=d["top_p"],
                       tool_delimiter=tool_delimiter)
    elif provider == "vllm_parsed":
        llm = OpenAILLM(client, model_id, temperature=d["temperature"])
    else:
        raise ValueError(provider)
    llm.name = "local"  # AgentDojo's convention for local models: the attack calls it "Local model"
    return llm, client


def build_pipeline(defence: str | None, llm, cfg: dict):
    """Baselines and shipped defences exactly as AgentPipeline.from_config builds them; CENE is the no-defence
    pipeline with CeneSession inserted after InitQuery."""
    from agentdojo.agent_pipeline import AgentPipeline, InitQuery, SystemMessage, ToolsExecutionLoop, ToolsExecutor
    from agentdojo.agent_pipeline.agent_pipeline import PipelineConfig, load_system_message

    from .adapter import CeneSession

    session = None
    if defence == "cene":
        session = CeneSession()
        sm = load_system_message(None if cfg["agentdojo"]["system_message"] == "default"
                                 else cfg["agentdojo"]["system_message"])
        loop = ToolsExecutionLoop([ToolsExecutor(), llm], max_iters=cfg["agentdojo"]["max_iters"])
        inner = AgentPipeline([SystemMessage(sm), InitQuery(), session, llm, loop])
        inner.name = "local-cene"
    else:
        inner = AgentPipeline.from_config(PipelineConfig(llm=llm, model_id=None, defense=defence,
                                                         system_message_name=None, system_message=None))
    rec = Recorder()
    pipe = AgentPipeline([inner, rec])
    pipe.name = inner.name
    return pipe, session, rec


class _GTName:
    """Named stand-in pipeline so the attack can resolve its model name for G2 (same as M4: "local-cene")."""

    name = "local-cene"


def build_gt_pipeline(task):
    from agentdojo.agent_pipeline import AgentPipeline
    from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline

    from .adapter import CeneSession

    session, rec = CeneSession(), Recorder()
    pipe = AgentPipeline([session, GroundTruthPipeline(task), rec])
    pipe.name = "local-cene"
    return pipe, session, rec


# ------------------------------------------------------------------ one episode
def trace_of(messages) -> list[tuple[str, dict]]:
    out = []
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                out.append((tc.function, dict(tc.args)))
    return out


def final_text(messages) -> str | None:
    from agentdojo.types import get_text_content_as_str

    if messages and messages[-1].get("role") == "assistant" and messages[-1].get("content") is not None:
        return get_text_content_as_str(messages[-1]["content"])
    return None


def run_episode(suite, cid: str, spec: dict, ut, it, attack_obj, llm_bundle, cfg: dict, built=None) -> dict:
    """built: (pipeline, session, recorder) built once per configuration (model configs only)."""
    from .hijack import is_hijacked, signatures

    mode = spec.get("mode")
    idx = episode_index(ut.ID, it.ID if it is not None else None)
    rec_out = {"config": cid, "user_task": ut.ID, "injection_task": it.ID if it is not None else None,
               "episode_index": idx, "seed": cfg["decoding"]["seed_base"] + idx}
    injections = attack_obj.attack(ut, it) if (attack_obj is not None and it is not None) else {}
    rec_out["injected_vectors"] = sorted(injections)
    if mode == "ground_truth_user":
        pipe, session, rec = build_gt_pipeline(ut)
    elif mode == "ground_truth_injection":
        pipe, session, rec = build_gt_pipeline(it)
    else:
        llm_bundle[1].errors.clear()
        llm_bundle[1].seconds.clear()
        llm_bundle[1].tokens.clear()
        llm_bundle[1].meta.clear()
        pipe, session, rec = built
        rec.messages = []
        if session is not None:
            session.reset()
    random.seed(rec_out["seed"])
    t0 = time.time()
    try:
        # G2: the token comes from the USER task's prompt and environment (as in M4), because
        # run_task_with_pipeline passes user_task.PROMPT as the query; the injection task's ground-truth
        # calls then run through that token on the injected environment.
        utility, security = suite.run_task_with_pipeline(pipe, ut, it, injections)
        rec_out.update(utility=bool(utility), security=None if it is None else bool(security), error=None)
    except Exception as e:
        rec_out.update(utility=False, security=None, error=f"{type(e).__name__}: {str(e)[:500]}",
                       traceback=traceback.format_exc()[-2000:],
                       security_agentdojo_convention=None if it is None else True)
    rec_out["seconds"] = round(time.time() - t0, 2)
    msgs = rec.messages
    trace = trace_of(msgs)
    rec_out["trace"] = [{"function": f, "args": a} for f, a in trace]
    rec_out["tool_calls"] = len(trace)
    rec_out["final_text"] = final_text(msgs)
    if mode is None:
        rec_out["llm_errors"] = list(llm_bundle[1].errors)
        rec_out["llm_call_seconds"] = list(llm_bundle[1].seconds)
        rec_out["llm_call_tokens"] = list(llm_bundle[1].tokens)
        rec_out["llm_call_meta"] = list(llm_bundle[1].meta)
        rec_out["parse_failures"] = sum((m or {}).get("parse_failures") or 0 for m in llm_bundle[1].meta)
        rec_out["model_calls"] = len(llm_bundle[1].seconds)
    if it is not None:
        env0 = suite.load_and_inject_default_environment(injections)
        env0 = ut.init_environment(env0)
        sigs = signatures(it.ground_truth(env0.model_copy(deep=True)), ut.ground_truth(env0.model_copy(deep=True)))
        hj, hits = is_hijacked(trace, sigs)
        rec_out.update(hijacked=hj, hijack_hits=hits, hijack_signatures=sigs)
    if session is not None:
        rec_out["cene"] = session.finish()
    rec_out["messages"] = [_jsonable_msg(m) for m in msgs]
    return rec_out


def _jsonable_msg(m) -> dict:
    out = {}
    for k, v in dict(m).items():
        if k in ("tool_calls",) and v is not None:
            out[k] = [{"function": tc.function, "args": dict(tc.args), "id": tc.id} for tc in v]
        elif k == "tool_call" and v is not None:
            out[k] = {"function": v.function, "args": dict(v.args)}
        else:
            out[k] = v
    return json.loads(json.dumps(out, default=str))


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("configs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--provider", default=None, help="native | local | vllm_parsed (M configs)")
    ap.add_argument("--tool-delimiter", default="tool")
    ap.add_argument("--model-id", default=None, help="served model name; default: first from /v1/models")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LOCAL_LLM_PORT", 8000)))
    ap.add_argument("--only", nargs="*", default=None,
                    help="restrict to user_task_N or user_task_N:injection_task_M (smoke runs)")
    ap.add_argument("--label", default=None, help="stored with the run, e.g. smoke")
    ap.add_argument("--stop-after-hours", type=float, default=None,
                    help="start no new episode after this many hours (Kaggle session limit); rerun to resume")
    ap.add_argument("--max-episodes", type=int, default=None, help="stop after this many new episodes (CHECK)")
    args = ap.parse_args(argv)

    import warnings
    warnings.filterwarnings("ignore")
    import agentdojo
    from importlib.metadata import version
    from agentdojo.attacks.attack_registry import load_attack
    from agentdojo.task_suite.load_suites import get_suite

    cfg = load_config()
    suite = get_suite(cfg["agentdojo"]["benchmark_version"], cfg["agentdojo"]["suite"])
    out = Path(args.out)
    doc = json.loads(out.read_text()) if out.exists() else {"runs": [], "episodes": {}}
    run_meta = {"started": dt.datetime.now(dt.timezone.utc).isoformat(), "configs": args.configs,
                "label": args.label, "only": args.only, "git": git_info(), "agentdojo": version("agentdojo"),
                "python": sys.version.split()[0], "platform": platform.platform(),
                "provider": args.provider, "tool_delimiter": args.tool_delimiter, "resumed": bool(doc["episodes"]),
                "run_config": cfg}
    llm_bundle = None
    if any("mode" not in cfg["matrix"][c] for c in args.configs):
        import requests
        model_id = args.model_id or requests.get(f"http://localhost:{args.port}/v1/models").json()["data"][0]["id"]
        run_meta["model_id"] = model_id
        llm_bundle = make_llm(args.provider or "local", model_id, args.tool_delimiter, cfg, args.port)
    doc["runs"].append(run_meta)

    user_tasks = sorted(suite.user_tasks.values(), key=lambda t: _num(t.ID))
    inj_tasks = sorted(suite.injection_tasks.values(), key=lambda t: _num(t.ID))
    only = set(args.only) if args.only else None
    t_start, new_eps, stopped = time.time(), 0, None
    for cid in args.configs:
        if stopped:
            break
        spec = cfg["matrix"][cid]
        attack_obj, built = None, None
        if not spec.get("mode"):
            built = build_pipeline(spec["defence"], llm_bundle[0], cfg)   # once per configuration
        if spec.get("attack"):
            attack_obj = load_attack(spec["attack"], suite, _GTName() if spec.get("mode") else built[0])
        pairs = [(ut, it) for ut in user_tasks for it in (inj_tasks if spec.get("attack") else [None])]
        for ut, it in pairs:
            key = f"{cid}|{ut.ID}|{it.ID if it else '-'}"
            if only and ut.ID not in only and f"{ut.ID}:{it.ID if it else ''}" not in only:
                continue
            if key in doc["episodes"]:
                continue
            if args.stop_after_hours is not None and (time.time() - t_start) / 3600 >= args.stop_after_hours:
                stopped = "deadline"
                break
            if args.max_episodes is not None and new_eps >= args.max_episodes:
                stopped = "max_episodes"
                break
            new_eps += 1
            ep = run_episode(suite, cid, spec, ut, it, attack_obj, llm_bundle, cfg, built)
            doc["episodes"][key] = ep
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=1, default=str))
            os.replace(tmp, out)
            c = ep.get("cene") or {}
            print(f"{key}: utility={ep['utility']} security={ep['security']} hijacked={ep.get('hijacked')} "
                  f"held={c.get('write_held')} denied={c.get('write_denied')} err={ep['error']}", flush=True)
    doc["runs"][-1]["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    doc["runs"][-1]["stopped"] = stopped          # None = everything requested is done
    doc["runs"][-1]["new_episodes"] = new_eps
    if stopped:
        print(f"STOPPED ({stopped}) after {new_eps} new episodes; rerun with the same --out to resume", flush=True)
    out.write_text(json.dumps(doc, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
