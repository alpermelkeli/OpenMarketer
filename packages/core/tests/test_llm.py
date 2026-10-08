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
    assert seen["body"]["model"] == model.router.resolve("repo_analyzer").model
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


ECHOED = "Authorization: Bearer key-echoed-by-an-error-page"


def failure_of(response: httpx.Response) -> LLMError:
    with pytest.raises(LLMError) as failure:
        client(lambda request: response).chat("writer", [{"role": "user", "content": "x"}])
    return failure.value


def test_refusal_says_the_status_and_not_what_the_provider_wrote():
    failure = failure_of(httpx.Response(401, text=f"<html>{ECHOED}</html>"))
    assert str(failure) == "the model provider answered HTTP 401"
    assert failure.status == 401


def test_error_inside_a_reply_is_not_repeated_either():
    failure = failure_of(httpx.Response(200, json={"error": {"message": ECHOED, "code": 429}}))
    assert str(failure) == "the model provider reported an error"
    assert failure.status == 429


def test_what_the_provider_wrote_is_logged_for_the_operator(caplog):
    with caplog.at_level("WARNING", logger="openmarketer_core.llm"):
        failure_of(httpx.Response(402, json={"error": {"message": "insufficient credits"}}))
    assert "insufficient credits" in caplog.text


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_failure_that_can_pass(status):
    assert LLMError("x", status=status).may_pass


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_refusal_of_the_request_itself_will_not_pass(status):
    assert not LLMError("x", status=status).may_pass


def test_provider_that_did_not_answer_may_answer_later():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMError) as failure:
        client(handler).chat("writer", [{"role": "user", "content": "x"}])
    assert (failure.value.status, failure.value.may_pass) == (None, True)
