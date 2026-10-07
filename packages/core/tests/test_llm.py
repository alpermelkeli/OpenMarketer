"""Tests for the router-backed chat client, against a fake HTTP transport."""

import json
from pathlib import Path

import httpx
import pytest

from openmarketer_core.llm import LLMError, RouterChatModel
from openmarketer_core.llm_config import ModelRouter

MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
TOOL = {"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}


def client(handler) -> RouterChatModel:
    router = ModelRouter.from_file(MODELS_YAML, env={"OPENROUTER_API_KEY": "test-key"})
    return RouterChatModel(router=router, transport=httpx.MockTransport(handler))


def test_request_and_reply():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "anthropic/claude-opus-5.5",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning": "dropped",
                            "tool_calls": [
                                {"id": "1", "function": {"name": "t", "arguments": "{}"}}
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.0042},
            },
        )

    model = client(handler)
    reply = model.chat("repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL])

    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "anthropic/claude-opus-5.5"
    assert seen["body"]["tools"] == [TOOL]
    assert seen["body"]["usage"] == {"include": True}
    assert "tool_choice" not in seen["body"]
    assert reply.message == {
        "role": "assistant",
        "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "{}"}}],
    }
    assert (reply.cost_usd, reply.prompt_tokens, reply.completion_tokens) == (0.0042, 120, 30)
    assert reply.tool_calls and reply.text == ""
    assert model.calls == [reply]


def test_role_without_tools_capability_cannot_get_tools():
    model = client(lambda request: httpx.Response(500))
    with pytest.raises(ValueError, match="not allowed to use tools"):
        model.chat("reader", [{"role": "user", "content": "x"}], tools=[TOOL])


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(402, json={"error": {"message": "insufficient credits"}}),
        httpx.Response(200, json={"error": {"message": "no endpoints found"}}),
        httpx.Response(200, json={"choices": []}),
    ],
)
def test_provider_failures_raise(response):
    with pytest.raises(LLMError):
        client(lambda request: response).chat("writer", [{"role": "user", "content": "x"}])


def test_network_failure_raises_without_leaking_details():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(LLMError, match="request failed: ConnectError"):
        client(handler).chat("writer", [{"role": "user", "content": "x"}])
