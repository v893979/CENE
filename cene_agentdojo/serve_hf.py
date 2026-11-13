"""Minimal OpenAI-compatible chat server on Hugging Face Transformers (run setup r2 option b; r3: native tools).

    python -m cene_agentdojo.serve_hf --model /kaggle/input/.../gemma-4-12b-it/1 --name gemma-4-12b-it \
        --port 8000 --max-memory-gib 14 --info-out /kaggle/working/serve_info.json

Why not `transformers serve`: in transformers 5.22 it always loads the multimodal model class
(AutoModelForMultimodalLM), so the vision/audio towers cannot be left out, and it has no per-GPU memory
limit for device_map="auto". Everything else here follows the same rules: temperature 0 -> greedy
(do_sample=False; top_p then has no effect), a request `seed` sets torch.manual_seed.

Endpoints: GET /v1/models, GET /stats, POST /v1/chat/completions (non-streaming; one request at a time).
A chat-template error is returned as HTTP 400 with the message, so the client records it.

r3 (native tool calling, thinking off):
* The OpenAI `tools` field is passed to the model's own chat template (apply_chat_template(..., tools=...));
  assistant `tool_calls` and role "tool" results are passed through in the OpenAI/HF message format, so the
  template renders them in its own syntax. `developer` (AgentDojo's OpenAI system role) becomes `system`.
* Thinking is fixed off: enable_thinking=False is passed to the template, and the thinking channel
  (<|channel>thought ... <channel|>) is removed from every reply before anything else; its length is recorded.
* The reply is decoded WITH special tokens and parsed by Transformers' own parser
  (transformers.utils.chat_parsing.parse_response) with the Gemma 4 response template that Transformers 5.22.2
  ships (transformers/cli/serving/utils.py). Calls become OpenAI `tool_calls`; if parsing fails the raw text
  is kept as content and the failure is counted (x_cene.parse_failures, GET /stats).

r4-model2 (second model): the reply format is chosen per model family, from the checkpoint's config
`model_type` (or --family). Gemma 4 is unchanged. Qwen 3 uses Transformers 5.22.2's own `qwen3` response template
(<tool_call>{"name": ..., "arguments": {...}}</tool_call>; reasoning in <think>...</think>). Thinking is off
(enable_thinking=False; Qwen's template then pre-fills an empty think block), and any think block, or a stray
</think> left by the pre-fill, is stripped from the reply and recorded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer


# Gemma 4 response template, copied from transformers 5.22.2 transformers/cli/serving/utils.py
# (_RESPONSE_TEMPLATE_FALLBACKS[("gemma4",)], Apache-2.0). Used only if that module cannot be imported.
# Copyright 2026 The HuggingFace Team. All rights reserved. Licensed under the Apache License, Version 2.0.
GEMMA4_RESPONSE_TEMPLATE = {
    "defaults": {"role": "assistant"},
    "start_anchor": ["<|turn>model\n", "<tool_response|>"],
    "fields": {
        "reasoning_content": {"open": "<|channel>thought\n", "close": "<channel|>", "content": "text"},
        "tool_calls": {
            "open_pattern": r"<\|tool_call>call:(?P<name>\w+)",
            "close": "<tool_call|>",
            "repeats": True,
            "content": "json",
            "content_args": {"unquoted_keys": True, "string_delims": [['<|"|>', '<|"|>']]},
            "transform": {"type": "function", "function": {"name": "{name}", "arguments": "{content}"}},
        },
        "content": {"close": ["<turn|>", "<|tool_response>", "<eos>"], "content": "text"},
    },
}
TOOL_CALL_OPEN = "<|tool_call>"
THINKING_RE = re.compile(r"<\|channel>thought\n.*?(?:<channel\|>|$)", re.S)

# Qwen 2/2.5/3 response template, copied from transformers 5.22.2 transformers/cli/serving/utils.py
# (_RESPONSE_TEMPLATE_FALLBACKS[(..., "qwen3", ...)], Apache-2.0). Used only if that module cannot be imported.
# Copyright 2026 The HuggingFace Team. All rights reserved. Licensed under the Apache License, Version 2.0.
QWEN3_RESPONSE_TEMPLATE = {
    "defaults": {"role": "assistant"},
    "start_anchor": "<|im_start|>assistant\n",
    "fields": {
        "reasoning_content": {"open": "<think>", "close": "</think>", "content": "text"},
        "tool_calls": {"open_pattern": r"\s*<tool_call>", "close": "</tool_call>", "repeats": True,
                       "content": "json", "transform": {"type": "function", "function": "{content}"}},
        "content": {"close_pattern": r"\s*(?:<\|im_end\|>|<\|endoftext\|>|<\|eot_id\|>)", "content": "text"},
    },
}

# Per-family reply format: which Transformers template, how thinking looks, and how a native call opens.
FAMILIES = {
    "gemma4": {"template_key": "gemma4", "vendored": GEMMA4_RESPONSE_TEMPLATE, "thinking_re": THINKING_RE,
               "tool_open": TOOL_CALL_OPEN, "stray_think_close": None},
    "qwen3": {"template_key": "qwen3", "vendored": QWEN3_RESPONSE_TEMPLATE,
              "thinking_re": re.compile(r"<think>.*?(?:</think>|$)", re.S),
              "tool_open": "<tool_call>", "stray_think_close": "</think>"},
}
MODEL_TYPE_TO_FAMILY = {"gemma4": "gemma4", "gemma4_text": "gemma4", "gemma4_unified": "gemma4",
                        "gemma4_unified_text": "gemma4", "qwen3": "qwen3", "qwen2": "qwen3"}


def family_for(model_type: str) -> str:
    if model_type in MODEL_TYPE_TO_FAMILY:
        return MODEL_TYPE_TO_FAMILY[model_type]
    raise ValueError(f"no reply format for model_type {model_type!r}; supported: {sorted(MODEL_TYPE_TO_FAMILY)}")


def response_template(family: str = "gemma4") -> tuple[dict, str]:
    f = FAMILIES[family]
    try:
        from transformers.cli.serving.utils import _RESPONSE_TEMPLATE_FALLBACKS
        tpl = next(v for k, v in _RESPONSE_TEMPLATE_FALLBACKS.items() if f["template_key"] in k)
        return tpl, "transformers.cli.serving.utils"
    except Exception:
        return f["vendored"], "vendored copy of transformers 5.22.2"


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


def normalize_messages(messages: list[dict]) -> list[dict]:
    """OpenAI chat messages -> the HF chat-template message format."""
    out = []
    for m in messages:
        role = "system" if m.get("role") == "developer" else m.get("role")
        n = {"role": role, "content": _text(m.get("content"))}
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                calls.append({"type": "function", "id": tc.get("id"),
                              "function": {"name": fn.get("name"), "arguments": args}})
            n["tool_calls"] = calls
        if role == "tool":
            for k in ("name", "tool_call_id"):
                if m.get(k) is not None:
                    n[k] = m[k]
        out.append(n)
    return out


def strip_specials(text: str, specials) -> str:
    for t in sorted(specials, key=len, reverse=True):
        text = text.replace(t, "")
    return text


def parse_output(raw: str, prefix: str, specials, tools_requested: bool, tools=None, family: str = "gemma4") -> dict:
    """raw: the generated text decoded with special tokens. Returns content, tool_calls and counters."""
    fam = FAMILIES[family]
    thinking = fam["thinking_re"].findall(raw)
    no_think = fam["thinking_re"].sub("", raw)
    stray = fam["stray_think_close"]
    if stray and stray in no_think:  # the template pre-filled "<think>": the reply starts inside a think block
        head, no_think = no_think.rsplit(stray, 1)
        thinking.append(head + stray)
    n_open = no_think.count(fam["tool_open"])
    meta = {"reasoning_chars": sum(len(t) for t in thinking), "thinking_regions": len(thinking),
            "native_call_opens": n_open, "parse_failures": 0, "parse_error": None}
    clean = strip_specials(no_think, specials).strip()
    if not tools_requested:
        # Prompt-based (AgentDojo LocalLLM) modes: the text as the model wrote it, minus thinking and special tokens.
        meta["native_calls_ignored"] = n_open
        return {"content": clean, "tool_calls": [], **meta}
    template, _ = response_template(family)
    try:
        from transformers.utils.chat_parsing.response_parser import parse_response
        parsed = parse_response(no_think, template, prefix=prefix, tools=tools)
        calls = []
        for c in parsed.get("tool_calls") or []:
            fn = c["function"]
            args = fn["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                raise ValueError(f"arguments of {fn.get('name')} are not an object")
            calls.append({"name": fn["name"], "arguments": json.dumps(args)})
        meta["parse_failures"] = max(0, n_open - len(calls))
        content = strip_specials(parsed.get("content") or "", specials).strip()
        return {"content": content, "tool_calls": calls, **meta}
    except Exception as e:
        meta["parse_failures"] = max(1, n_open)
        meta["parse_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        return {"content": clean, "tool_calls": [], **meta}


class BadRequest(Exception):
    """Returned to the client as HTTP 400 (e.g. the chat template rejects the conversation's roles)."""


def generation_kwargs(temperature, top_p, max_new_tokens: int) -> dict:
    """Greedy when temperature is 0 or missing, exactly as vLLM and `transformers serve` treat it."""
    if temperature is None or float(temperature) == 0.0:
        return {"do_sample": False, "max_new_tokens": int(max_new_tokens), "temperature": None, "top_p": None,
                "top_k": None}
    kw = {"do_sample": True, "temperature": float(temperature), "max_new_tokens": int(max_new_tokens)}
    if top_p is not None:
        kw["top_p"] = float(top_p)
    return kw


class HFBackend:
    """Loads the model once. Text-only first (the CausalLM class for the checkpoint's text config, with the
    language-model weights mapped across); if any language-model weight is missing it falls back to the full
    model class and records that."""

    def __init__(self, path: str, name: str, dtype: str = "float16", attn_implementation: str = "sdpa",
                 text_only: bool = True, max_memory_gib: float | None = None, max_new_tokens: int = 1024,
                 family: str | None = None):
        import torch
        import transformers
        from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.name = name
        self.max_new_tokens = max_new_tokens
        t0 = time.time()
        torch_dtype = getattr(torch, dtype)
        n_gpu = torch.cuda.device_count()
        common = {"device_map": "auto" if n_gpu else None, "attn_implementation": attn_implementation}
        if max_memory_gib and n_gpu:
            common["max_memory"] = {i: f"{max_memory_gib}GiB" for i in range(n_gpu)}
        self.info = {"stack": "transformers", "transformers": transformers.__version__, "torch": torch.__version__,
                     "cuda_devices": [torch.cuda.get_device_name(i) for i in range(n_gpu)], "dtype": dtype,
                     "attn_implementation": attn_implementation, "max_memory_gib": max_memory_gib,
                     "max_new_tokens": max_new_tokens, "path": path, "name": name}
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        if getattr(self.tokenizer, "chat_template", None) is None:
            from transformers import AutoProcessor
            self.tokenizer.chat_template = AutoProcessor.from_pretrained(path).chat_template
        tpl = self.tokenizer.chat_template if isinstance(self.tokenizer.chat_template, str) else json.dumps(
            self.tokenizer.chat_template)
        self.specials = sorted({t.content for t in self.tokenizer.added_tokens_decoder.values() if t.special}
                               | set(self.tokenizer.all_special_tokens))
        self.info["chat_template_sha256"] = hashlib.sha256(tpl.encode()).hexdigest()
        self.info["chat_template_mentions_enable_thinking"] = "enable_thinking" in tpl
        self.info["thinking"] = ("off: enable_thinking=False passed to the template; the thinking channel is also "
                                 "stripped from every reply")
        cfg = AutoConfig.from_pretrained(path)
        self.family = family or family_for(cfg.model_type)
        self.info["model_type"] = cfg.model_type
        self.info["family"] = self.family
        self.info["response_template_source"] = response_template(self.family)[1]
        model = None
        text_cfg = getattr(cfg, "text_config", None)
        if text_cfg is None:  # a text-only checkpoint (e.g. Qwen 3): nothing to leave out
            model = AutoModelForCausalLM.from_pretrained(path, dtype=torch_dtype, **common)
            self.info["text_only_result"] = "not applicable: text-only checkpoint"
        if text_only and text_cfg is not None:
            try:
                cls = AutoModelForCausalLM._model_mapping[type(text_cfg)]
                model, li = cls.from_pretrained(path, config=text_cfg, dtype=torch_dtype, output_loading_info=True,
                                                key_mapping={r"^model\.language_model\.": "model."}, **common)
                missing = [k for k in li.get("missing_keys", []) if not k.startswith("lm_head")]
                self.info["text_only_missing_keys"] = missing[:20]
                if missing:
                    del model
                    model = None
                    torch.cuda.empty_cache()
                    self.info["text_only_result"] = "rejected: missing language-model weights"
                else:
                    self.info["text_only_result"] = "ok"
            except Exception as e:  # class or mapping not supported in this version
                model = None
                self.info["text_only_result"] = f"failed: {type(e).__name__}: {str(e)[:300]}"
        if model is None:
            from transformers import AutoModelForImageTextToText
            try:
                model = AutoModelForImageTextToText.from_pretrained(path, dtype=torch_dtype, **common)
            except (ValueError, KeyError):
                model = AutoModelForCausalLM.from_pretrained(path, dtype=torch_dtype, **common)
        model.eval()
        self.model = model
        self.info.update(model_class=type(model).__name__,
                         text_only=self.info.get("text_only_result") in ("ok", "not applicable: text-only checkpoint"),
                         device_map=sorted({str(v) for v in (getattr(model, "hf_device_map", None) or {}).values()}),
                         load_seconds=round(time.time() - t0, 1))

    def generate(self, messages, temperature=None, top_p=None, seed=None, max_tokens=None, tools=None):
        torch = self.torch
        try:
            enc = self.tokenizer.apply_chat_template(normalize_messages(messages), tools=tools or None,
                                                     add_generation_prompt=True, return_tensors="pt",
                                                     return_dict=True, enable_thinking=False)
        except Exception as e:
            raise BadRequest(f"chat template: {type(e).__name__}: {e}") from e
        dev = next(self.model.parameters()).device
        enc = {k: v.to(dev) for k, v in enc.items()}
        if seed is not None:
            torch.manual_seed(int(seed))
        kw = generation_kwargs(temperature, top_p, max_tokens or self.max_new_tokens)
        with torch.inference_mode():
            out = self.model.generate(**enc, **kw, pad_token_id=self.tokenizer.pad_token_id
                                      or self.tokenizer.eos_token_id)
        n_in = enc["input_ids"].shape[1]
        new = out[0, n_in:]
        raw = self.tokenizer.decode(new, skip_special_tokens=False)
        prefix = self.tokenizer.decode(enc["input_ids"][0], skip_special_tokens=False)
        finish = "length" if new.shape[0] >= kw["max_new_tokens"] else "stop"
        return {"raw": raw, "prefix": prefix, "prompt_tokens": int(n_in), "completion_tokens": int(new.shape[0]),
                "finish": finish}


def make_handler(backend):
    stats = {"requests": 0, "with_tools": 0, "tool_calls": 0, "parse_failures": 0, "replies_with_thinking": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") in ("/v1/models", "/models"):
                return self._send(200, {"object": "list", "data": [{"id": backend.name, "object": "model"}]})
            if self.path.rstrip("/") == "/health":
                return self._send(200, {"ok": True})
            if self.path.rstrip("/") == "/stats":
                return self._send(200, stats)
            self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                return self._send(404, {"error": {"message": "not found"}})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if req.get("stream"):
                    raise BadRequest("streaming is not supported")
                tools = req.get("tools") or None
                if tools and req.get("tool_choice") not in (None, "auto"):
                    raise BadRequest("only tool_choice auto is supported")
                msgs = req.get("messages")
                if not isinstance(msgs, list) or not msgs:
                    raise BadRequest("messages must be a non-empty list")
                t0 = time.time()
                g = backend.generate(msgs, req.get("temperature"), req.get("top_p"), req.get("seed"),
                                     req.get("max_tokens"), tools)
                out = parse_output(g["raw"], g["prefix"], backend.specials, bool(tools), tools,
                                   getattr(backend, "family", "gemma4"))
                message = {"role": "assistant", "content": out["content"]}
                finish = g["finish"]
                if out["tool_calls"]:
                    message["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                                              "function": {"name": c["name"], "arguments": c["arguments"]}}
                                             for c in out["tool_calls"]]
                    finish = "tool_calls"
                stats["requests"] += 1
                stats["with_tools"] += bool(tools)
                stats["tool_calls"] += len(out["tool_calls"])
                stats["parse_failures"] += out["parse_failures"]
                stats["replies_with_thinking"] += out["thinking_regions"] > 0
                x = {k: out.get(k) for k in ("parse_failures", "parse_error", "reasoning_chars", "thinking_regions",
                                             "native_call_opens", "native_calls_ignored")}
                x.update(raw=g["raw"][:4000], seconds=round(time.time() - t0, 3))
                self._send(200, {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion", "created": int(time.time()),
                    "model": backend.name,
                    "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                    "usage": {"prompt_tokens": g["prompt_tokens"], "completion_tokens": g["completion_tokens"],
                              "total_tokens": g["prompt_tokens"] + g["completion_tokens"]},
                    "x_cene": x})
            except BadRequest as e:
                self._send(400, {"error": {"message": str(e), "type": "invalid_request_error"}})
            except Exception as e:
                self._send(500, {"error": {"message": f"{type(e).__name__}: {e}", "type": "server_error",
                                           "trace": traceback.format_exc()[-1500:]}})

    return Handler


def serve(backend, host: str = "127.0.0.1", port: int = 8000) -> HTTPServer:
    return HTTPServer((host, port), make_handler(backend))  # single-threaded: one request at a time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dtype", default="float16")
    ap.add_argument("--attn-implementation", default="sdpa")
    ap.add_argument("--no-text-only", action="store_true")
    ap.add_argument("--max-memory-gib", type=float, default=None)
    ap.add_argument("--max-new-tokens", type=int, default=1024)
    ap.add_argument("--info-out", default=None)
    ap.add_argument("--family", default=None, choices=sorted(FAMILIES), help="default: from the config model_type")
    a = ap.parse_args(argv)
    backend = HFBackend(a.model, a.name, a.dtype, a.attn_implementation, not a.no_text_only, a.max_memory_gib,
                        a.max_new_tokens, a.family)
    print(json.dumps(backend.info), flush=True)
    if a.info_out:
        with open(a.info_out, "w") as f:
            json.dump(backend.info, f, indent=1)
    srv = serve(backend, "127.0.0.1", a.port)
    print(f"serving {a.name} on http://127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
