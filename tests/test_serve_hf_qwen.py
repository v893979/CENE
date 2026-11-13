"""Qwen 3 reply format in cene_agentdojo/serve_hf.py (run setup r4-model2), with a fake backend (no torch).

Replies follow Qwen 3's chat-template format, as encoded in the response template Transformers 5.22.2 ships
(transformers/cli/serving/utils.py): tool calls as <tool_call>\\n{"name": ..., "arguments": {...}}\\n</tool_call>,
the turn ends with <|im_end|>, and with enable_thinking=False the template pre-fills an empty
"<think>\\n\\n</think>\\n\\n" after "<|im_start|>assistant\\n".
"""
from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from cene_agentdojo.serve_hf import family_for, parse_output, response_template, serve

pytest.importorskip("transformers")

SPECIALS = ["<|im_start|>", "<|im_end|>", "<|endoftext|>"]
PREFIX_THINK_OFF = ("<|im_start|>system\nsys<|im_end|>\n<|im_start|>user\nPay the bill<|im_end|>\n"
                    "<|im_start|>assistant\n<think>\n\n</think>\n\n")
PREFIX_PLAIN = "<|im_start|>system\nsys<|im_end|>\n<|im_start|>user\nPay the bill<|im_end|>\n<|im_start|>assistant\n"
ONE_CALL = '<tool_call>\n{"name": "read_file", "arguments": {"file_path": "bill-december-2023.txt"}}\n</tool_call><|im_end|>'
TWO_CALLS = ('<tool_call>\n{"name": "send_money", "arguments": {"recipient": "GB29NWBK60161331926819", "amount": 4.0, '
             '"subject": "refund", "date": "2022-03-08"}}\n</tool_call>\n<tool_call>\n{"name": "get_balance", '
             '"arguments": {}}\n</tool_call><|im_end|>')
TEXT = "Your total spending in March 2022 was 1050.00.<|im_end|>"
LEAKED_THINK = "<think>\nThe user wants the bill paid.\n</think>\n\n" + ONE_CALL
STRAY_CLOSE = "The user wants the bill paid.\n</think>\n\n" + ONE_CALL
BROKEN = '<tool_call>\n{"name": "send_money", "arguments": {"recipient": "GB29", "amount": \n</tool_call><|im_end|>'


def test_family_from_model_type():
    assert family_for("qwen3") == "qwen3" and family_for("qwen2") == "qwen3"
    assert family_for("gemma4") == "gemma4" and family_for("gemma4_unified_text") == "gemma4"
    with pytest.raises(ValueError):
        family_for("llama")
    assert response_template("qwen3")[0]["start_anchor"] == "<|im_start|>assistant\n"


def test_one_call_with_thinking_prefilled_off():
    out = parse_output(ONE_CALL, PREFIX_THINK_OFF, SPECIALS, True, family="qwen3")
    assert out["tool_calls"] == [{"name": "read_file", "arguments": json.dumps({"file_path": "bill-december-2023.txt"})}]
    assert out["parse_failures"] == 0 and out["thinking_regions"] == 0 and out["content"] == ""


def test_two_calls():
    out = parse_output(TWO_CALLS, PREFIX_THINK_OFF, SPECIALS, True, family="qwen3")
    assert [c["name"] for c in out["tool_calls"]] == ["send_money", "get_balance"]
    assert json.loads(out["tool_calls"][0]["arguments"])["amount"] == 4.0
    assert out["native_call_opens"] == 2 and out["parse_failures"] == 0


def test_text_answer():
    out = parse_output(TEXT, PREFIX_THINK_OFF, SPECIALS, True, family="qwen3")
    assert out["content"] == "Your total spending in March 2022 was 1050.00." and out["tool_calls"] == []


def test_thinking_that_leaks_is_stripped_and_recorded():
    out = parse_output(LEAKED_THINK, PREFIX_PLAIN, SPECIALS, True, family="qwen3")
    assert out["thinking_regions"] == 1 and out["reasoning_chars"] > 0
    assert [c["name"] for c in out["tool_calls"]] == ["read_file"] and "bill paid" not in out["content"]


def test_stray_think_close_is_stripped_and_recorded():
    out = parse_output(STRAY_CLOSE, PREFIX_PLAIN, SPECIALS, True, family="qwen3")
    assert out["thinking_regions"] == 1 and [c["name"] for c in out["tool_calls"]] == ["read_file"]


def test_broken_call_is_counted_and_raw_kept():
    out = parse_output(BROKEN, PREFIX_THINK_OFF, SPECIALS, True, family="qwen3")
    assert out["tool_calls"] == [] and out["parse_failures"] == 1 and out["parse_error"]
    assert "send_money" in out["content"]


def test_prompt_based_mode_keeps_the_text():
    out = parse_output(ONE_CALL, PREFIX_THINK_OFF, SPECIALS, False, family="qwen3")
    assert out["tool_calls"] == [] and out["native_calls_ignored"] == 1
    assert out["content"].startswith("<tool_call>") and "<|im_end|>" not in out["content"]


def test_gemma_default_is_unchanged():
    gemma = '<|tool_call>call:get_balance{}<tool_call|>'
    out = parse_output(gemma, "<|turn>model\n", ["<|tool_call>", "<tool_call|>"], True)
    assert [c["name"] for c in out["tool_calls"]] == ["get_balance"]


class QwenFakeBackend:
    name = "qwen3-8b"
    family = "qwen3"
    specials = SPECIALS

    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def generate(self, messages, temperature=None, top_p=None, seed=None, max_tokens=None, tools=None):
        self.calls.append({"messages": messages, "tools": tools, "temperature": temperature})
        return {"raw": self.raw, "prefix": PREFIX_THINK_OFF, "prompt_tokens": 9, "completion_tokens": 5,
                "finish": "stop"}


def test_agentdojo_native_provider_with_qwen():
    """AgentDojo's own OpenAI-compatible LLM element, unchanged, gets Qwen's call as a tool call."""
    pytest.importorskip("agentdojo")
    import openai
    from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
    from agentdojo.functions_runtime import FunctionsRuntime
    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.types import ChatSystemMessage, ChatUserMessage, text_content_block_from_string

    be = QwenFakeBackend(ONE_CALL)
    srv = serve(be, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        llm = OpenAILLM(openai.OpenAI(api_key="EMPTY", base_url=url + "/v1"), "qwen3-8b", temperature=0.0)
        suite = get_suite("v1.2.2", "banking")
        msgs = [ChatSystemMessage(role="system", content=[text_content_block_from_string("sys")]),
                ChatUserMessage(role="user", content=[text_content_block_from_string("Pay the bill")])]
        _, _, _, out, _ = llm.query("q", FunctionsRuntime(suite.tools), None, msgs)
        tc = out[-1]["tool_calls"][0]
        assert tc.function == "read_file" and tc.args == {"file_path": "bill-december-2023.txt"}
        assert be.calls[0]["temperature"] is None and len(be.calls[0]["tools"]) == len(suite.tools)
        with urllib.request.urlopen(url + "/stats", timeout=10) as r:
            assert json.loads(r.read())["tool_calls"] == 1
    finally:
        srv.shutdown()
        srv.server_close()
