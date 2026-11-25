"""Responses-API adapter: mapping, parsing, non-stream and SSE contracts (no network)."""
from __future__ import annotations

import json

import httpx
import pytest

from argo.config import ArgoConfig, ModelConfig, ProviderConfig
from argo.core.types import Message, ToolCall, Usage
from argo.llm.client import LLMClient, messages_to_responses, parse_responses, responses_tools


def cfg(wire_api="responses") -> ArgoConfig:
    return ArgoConfig(provider=ProviderConfig(base_url="http://test/v1", api_key="x",
                                              model="test-model", wire_api=wire_api),
                      models=ModelConfig(max_output_tokens=256, temperature=0.2))


RESPONSE_JSON = {
    "id": "resp_1", "object": "response", "model": "test-model", "status": "completed",
    "output": [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "think "}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "hello"}]},
        {"type": "function_call", "call_id": "c1", "name": "calc", "arguments": '{"x": 1}'},
    ],
    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
              "input_tokens_details": {"cached_tokens": 3},
              "output_tokens_details": {"reasoning_tokens": 2}},
}

SSE = "\n\n".join([
    'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1"}}',
    'event: response.reasoning_text.delta\ndata: {"type":"response.reasoning_text.delta","delta":"think"}',
    'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hel"}',
    'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"lo"}',
    'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":2,"item":{"type":"function_call","call_id":"c1","name":"calc","arguments":""}}',
    'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","output_index":2,"delta":"{\\"x\\": "}',
    'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","output_index":2,"delta":"1}"}',
    'event: response.output_item.done\ndata: {"type":"response.output_item.done","output_index":2,"item":{"type":"function_call","call_id":"c1","name":"calc","arguments":"{\\"x\\": 1}"}}',
    'event: response.completed\ndata: ' + json.dumps({"type": "response.completed", "response": RESPONSE_JSON}),
]) + "\n\n"


def test_message_mapping_replays_tool_history():
    messages = [
        Message(role="system", content="be terse"),
        Message(role="user", content="calc it"),
        Message(role="assistant", content="working", tool_calls=[ToolCall(id="c1", name="calc", arguments='{"x":1}')]),
        Message(role="tool", content="1", tool_call_id="c1", name="calc"),
    ]
    instructions, items = messages_to_responses(messages)
    assert instructions == "be terse"
    assert items[0]["role"] == "user"
    assert items[1]["content"][0] == {"type": "input_text", "text": "working"}
    assert items[2] == {"type": "function_call", "call_id": "c1", "name": "calc", "arguments": '{"x":1}'}
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "1"}


def test_tool_schema_conversion_is_flat():
    tools = [{"type": "function", "function": {"name": "calc", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]
    out = responses_tools(tools)
    assert out[0]["type"] == "function" and out[0]["name"] == "calc" and "function" not in out[0]


def test_parse_response_and_usage():
    msg, finish = parse_responses(RESPONSE_JSON)
    assert msg.content == "hello" and msg.reasoning == "think " and finish == "tool_calls"
    assert msg.tool_calls[0].id == "c1" and msg.tool_calls[0].name == "calc"
    from argo.llm.client import usage_from_responses
    usage = usage_from_responses(RESPONSE_JSON["usage"])
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (10, 5, 15)
    assert usage.cached_tokens == 3 and usage.reasoning_tokens == 2


async def test_non_stream_responses_request_and_parse():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=RESPONSE_JSON)

    client = LLMClient(cfg(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tools = [{"type": "function", "function": {"name": "calc", "parameters": {"type": "object"}}}]
    resp = await client.chat([Message(role="system", content="sys"), Message(role="user", content="hi")], tools)
    assert seen["url"].endswith("/responses")
    body = seen["body"]
    assert body["instructions"] == "sys" and body["tools"][0]["name"] == "calc"
    assert body["max_output_tokens"] == 256 and body["temperature"] == 0.2
    assert resp.message.content == "hello" and resp.finish_reason == "tool_calls"
    assert resp.usage.total_tokens == 15 and resp.model == "test-model"


async def test_stream_responses_assembles_text_tools_and_usage():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    client = LLMClient(cfg(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    deltas = [d async for d in client.stream_chat([Message(role="user", content="hi")], None)]
    text = "".join(d.text for d in deltas)
    assert text == "hello"
    assert any(d.reasoning == "think" for d in deltas)
    final = deltas[-1].final
    assert final.message.tool_calls[0].arguments == '{"x": 1}'
    assert final.usage.total_tokens == 15 and final.finish_reason == "tool_calls"
