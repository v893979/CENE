"""Tests for the minimal OpenAI-compatible server (cene_agentdojo/serve_hf.py) with a fake backend (no torch).

Gemma 4 replies below follow the response template that transformers 5.22.2 ships for Gemma 4
(transformers/cli/serving/utils.py): thinking in <|channel>thought\\n ... <channel|>, tool calls as
<|tool_call>call:NAME{...}<tool_call|> with <|"|> string delimiters. The Kaggle CHECK (run setup r2) reply was
recorded with special tokens stripped; RAW_CHECK_REPLY is that reply with the template's tokens put back.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from cene_agentdojo.serve_hf import (BadRequest, generation_kwargs, normalize_messages, parse_output, serve,
                                     strip_specials)

SPECIALS = ["<|channel>", "<channel|>", "<|tool_call>", "<tool_call|>", '<|"|>', "<|turn>", "<turn|>", "<eos>",
            "<|tool_response>", "<tool_response|>", "<bos>"]
PREFIX = "<bos><|turn>system\nsys<turn|>\n<|turn>user\nPay the bill<turn|>\n<|turn>model\n"
RECORDED_CHECK_REPLY = 'thought\ncall:read_file{"file_path": "bill-december-2023.txt"}'   # Kaggle CHECK r2, verbatim
RAW_CHECK_REPLY = '<|channel>thought\n<channel|><|tool_call>call:read_file{"file_path": "bill-december-2023.txt"}<tool_call|>'
RAW_DELIMS = ('<|tool_call>call:send_money{recipient:<|"|>GB29NWBK60161331926819<|"|>,amount:4.0,'
              'subject:<|"|>refund<|"|>,date:<|"|>2022-03-08<|"|>}<tool_call|><|tool_call>call:get_balance{}<tool_call|>')
RAW_THINK_TEXT = "<|channel>thought\nThe user wants the total.<channel|>Your total spending was 1050.00.<turn|>"
RAW_TRUNCATED = '<|tool_call>call:send_money{recipient:<|"|>GB29<|"|>,amount:'

needs_parser = pytest.importorskip  # the native parser is transformers' own


# ------------------------------------------------------------------ parsing (no server)
def test_recorded_check_reply_shows_why_r2_failed():
    """Decoded without special tokens, the call markers are gone and nothing can be parsed (r2's failure)."""
    needs_parser("transformers")
    out = parse_output(RECORDED_CHECK_REPLY, PREFIX, SPECIALS, tools_requested=True)
    assert out["tool_calls"] == [] and out["native_call_opens"] == 0
    assert out["content"] == RECORDED_CHECK_REPLY


def test_raw_check_reply_parses_to_the_call():
    needs_parser("transformers")
    out = parse_output(RAW_CHECK_REPLY, PREFIX, SPECIALS, tools_requested=True)
    assert out["tool_calls"] == [{"name": "read_file", "arguments": json.dumps({"file_path": "bill-december-2023.txt"})}]
    assert out["parse_failures"] == 0 and out["thinking_regions"] == 1 and out["content"] == ""


def test_string_delimiters_and_two_calls():
    needs_parser("transformers")
    out = parse_output(RAW_DELIMS, PREFIX, SPECIALS, tools_requested=True)
    assert [c["name"] for c in out["tool_calls"]] == ["send_money", "get_balance"]
    assert json.loads(out["tool_calls"][0]["arguments"]) == {"recipient": "GB29NWBK60161331926819", "amount": 4.0,
                                                             "subject": "refund", "date": "2022-03-08"}
    assert json.loads(out["tool_calls"][1]["arguments"]) == {}


def test_thinking_is_stripped_and_recorded():
    needs_parser("transformers")
    out = parse_output(RAW_THINK_TEXT, PREFIX, SPECIALS, tools_requested=True)
    assert out["content"] == "Your total spending was 1050.00." and out["tool_calls"] == []
    assert out["thinking_regions"] == 1 and out["reasoning_chars"] > 0
    assert "user wants" not in out["content"]


def test_unparseable_call_is_counted_and_raw_kept():
    needs_parser("transformers")
    out = parse_output(RAW_TRUNCATED, PREFIX, SPECIALS, tools_requested=True)
    assert out["tool_calls"] == [] and out["parse_failures"] == 1 and out["parse_error"]
    assert "send_money" in out["content"]


def test_prompt_based_modes_get_text_without_thinking_or_special_tokens():
    out = parse_output(RAW_CHECK_REPLY, PREFIX, SPECIALS, tools_requested=False)
    assert out["content"] == 'call:read_file{"file_path": "bill-december-2023.txt"}'
    assert out["native_calls_ignored"] == 1 and out["tool_calls"] == []
    assert strip_specials("<|turn>a<turn|>", SPECIALS) == "a"


def test_openai_messages_become_template_messages():
    msgs = [{"role": "developer", "content": [{"type": "text", "text": "sys"}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_balance", "arguments": "{}"}}]},
            {"role": "tool", "content": [{"type": "text", "text": "1810.0"}], "tool_call_id": "call_1",
             "name": "get_balance"}]
    n = normalize_messages(msgs)
    assert [m["role"] for m in n] == ["system", "user", "assistant", "tool"]
    assert n[0]["content"] == "sys" and n[2]["tool_calls"][0]["function"]["arguments"] == {}
    assert n[3] == {"role": "tool", "content": "1810.0", "name": "get_balance", "tool_call_id": "call_1"}


def test_greedy_when_temperature_is_zero_or_missing():
    assert generation_kwargs(0.0, 0.9, 64)["do_sample"] is False
    assert generation_kwargs(None, None, 64)["do_sample"] is False   # AgentDojo's OpenAI client drops 0.0
    kw = generation_kwargs(0.7, 0.9, 64)
    assert kw["do_sample"] is True and kw["temperature"] == 0.7 and kw["top_p"] == 0.9


# ------------------------------------------------------------------ server with a fake backend
class FakeBackend:
    name = "fake-gemma"
    specials = SPECIALS

    def __init__(self, replies=(RAW_CHECK_REPLY,), reject_role=None):
        self.replies, self.reject_role, self.calls = list(replies), reject_role, []

    def generate(self, messages, temperature=None, top_p=None, seed=None, max_tokens=None, tools=None):
        if self.reject_role and any(m["role"] == self.reject_role for m in messages):
            raise BadRequest(f"chat template: TemplateError: role {self.reject_role} not supported")
        self.calls.append({"messages": messages, "temperature": temperature, "top_p": top_p, "seed": seed,
                           "tools": tools})
        raw = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"raw": raw, "prefix": PREFIX, "prompt_tokens": 11, "completion_tokens": 7, "finish": "stop"}


@pytest.fixture
def server():
    started = []

    def start(backend):
        srv = serve(backend, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in started:
        s.shutdown()
        s.server_close()


def _post(url, body):
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_models_and_chat_completion_shape(server):
    be = FakeBackend(replies=[RAW_THINK_TEXT])
    url = server(be)
    with urllib.request.urlopen(url + "/v1/models", timeout=10) as r:
        assert json.loads(r.read())["data"][0]["id"] == "fake-gemma"
    code, out = _post(url, {"model": "fake-gemma", "messages": [{"role": "user", "content": "hi"}],
                            "temperature": 0.0, "top_p": 0.9, "seed": 42})
    assert code == 200 and out["choices"][0]["message"]["content"] == "Your total spending was 1050.00."
    assert out["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert out["x_cene"]["thinking_regions"] == 1
    assert be.calls[0]["seed"] == 42 and be.calls[0]["temperature"] == 0.0 and be.calls[0]["tools"] is None


def test_template_errors_and_unsupported_fields_are_400(server):
    url = server(FakeBackend(reject_role="tool"))
    code, out = _post(url, {"messages": [{"role": "user", "content": "a"}, {"role": "tool", "content": "b"}]})
    assert code == 400 and "TemplateError" in out["error"]["message"]
    assert _post(url, {"messages": [{"role": "user", "content": "a"}], "stream": True})[0] == 400
    assert _post(url, {"messages": [{"role": "user", "content": "a"}], "tools": [{"type": "function"}],
                       "tool_choice": "required"})[0] == 400
    assert _post(url, {"messages": []})[0] == 400


def test_agentdojo_native_provider_gets_tool_calls(server):
    """AgentDojo's OpenAI-compatible LLM element (what vllm_parsed uses), unchanged, against this server."""
    needs_parser("transformers")
    pytest.importorskip("agentdojo")
    import openai
    from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
    from agentdojo.functions_runtime import FunctionsRuntime
    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.types import ChatSystemMessage, ChatUserMessage, text_content_block_from_string

    be = FakeBackend(replies=[RAW_CHECK_REPLY])
    url = server(be)
    llm = OpenAILLM(openai.OpenAI(api_key="EMPTY", base_url=url + "/v1"), "fake-gemma", temperature=0.0)
    suite = get_suite("v1.2.2", "banking")
    msgs = [ChatSystemMessage(role="system", content=[text_content_block_from_string("sys")]),
            ChatUserMessage(role="user", content=[text_content_block_from_string("Pay the bill")])]
    _, _, _, out, _ = llm.query("q", FunctionsRuntime(suite.tools), None, msgs)
    tc = out[-1]["tool_calls"][0]
    assert tc.function == "read_file" and tc.args == {"file_path": "bill-december-2023.txt"} and tc.id
    sent = be.calls[0]
    assert {t["function"]["name"] for t in sent["tools"]} == {t.name for t in suite.tools}
    assert sent["messages"][0]["role"] == "developer"          # mapped to system inside generate()
    assert sent["temperature"] is None                         # dropped by the client; greedy on the server
    with urllib.request.urlopen(url + "/stats", timeout=10) as r:
        assert json.loads(r.read())["tool_calls"] == 1


def test_agentdojo_local_llm_talks_to_it(server):
    """AgentDojo's prompt-based LocalLLM, unchanged: it only understands <function=...>, so a native call is not
    parsed (this is the r2 CHECK outcome); the text it gets has no thinking channel."""
    pytest.importorskip("agentdojo")
    import openai
    from agentdojo.agent_pipeline.llms.local_llm import LocalLLM
    from agentdojo.functions_runtime import FunctionsRuntime
    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.types import ChatSystemMessage, ChatUserMessage, text_content_block_from_string

    be = FakeBackend(replies=['<function=get_balance>{}</function>'])
    url = server(be)
    llm = LocalLLM(openai.OpenAI(api_key="EMPTY", base_url=url + "/v1"), "fake-gemma", temperature=0.0, top_p=0.9,
                   tool_delimiter="user")
    suite = get_suite("v1.2.2", "banking")
    msgs = [ChatSystemMessage(role="system", content=[text_content_block_from_string("sys")]),
            ChatUserMessage(role="user", content=[text_content_block_from_string("What is my balance?")])]
    _, _, _, out, _ = llm.query("q", FunctionsRuntime(suite.tools), None, msgs)
    assert out[-1]["tool_calls"][0].function == "get_balance"
    assert be.calls[0]["temperature"] == 0.0 and be.calls[0]["top_p"] == 0.9 and isinstance(be.calls[0]["seed"], int)
    be.replies = [RAW_CHECK_REPLY]
    _, _, _, out, _ = llm.query("q", FunctionsRuntime(suite.tools), None, msgs)
    assert out[-1]["tool_calls"] == [] and "thought" not in out[-1]["content"][0]["content"]
