"""Tests for the router-backed chat client, against a fake HTTP transport."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from openmarketer_core.llm import (
    MAX_IMAGE_PART_BYTES,
    ForcedChoiceRefusals,
    ImagePartRefused,
    LLMError,
    RouterChatModel,
    image_part,
)
from openmarketer_core.llm_config import ConfigError, ModelRouter

MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
TOOL = {"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}


def client(handler) -> RouterChatModel:
    router = ModelRouter.from_file(MODELS_YAML, env={"OPENROUTER_API_KEY": "test-key"})
    return RouterChatModel(
        router=router,
        transport=httpx.MockTransport(handler),
        forced_choice_refusals=ForcedChoiceRefusals(),
    )


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
                        },
                        "finish_reason": "tool_calls",
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
    assert reply.finish_reason == "tool_calls"
    assert model.calls == [reply]


def test_a_cut_off_answer_keeps_the_length_finish_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": None}, "finish_reason": "length"}
                ]
            },
        )

    reply = client(handler).chat("writer", [{"role": "user", "content": "hi"}])

    assert reply.finish_reason == "length"


def test_a_reply_without_a_finish_reason_keeps_none():
    reply = client(lambda request: reply_naming("provider/model")).chat(
        "writer", [{"role": "user", "content": "hi"}]
    )
    assert reply.finish_reason is None


def reply_naming(model: object) -> httpx.Response:
    message = {"role": "assistant", "content": "hello"}
    return httpx.Response(200, json={"model": model, "choices": [{"message": message}]})


@pytest.mark.parametrize(
    "reported",
    ["fake\x00model", "fake\nmodel", "", "m" * 201, None, {"name": "fake/model"}, 7],
    ids=["nul", "line-break", "empty", "too-long", "missing", "object", "number"],
)
def test_model_name_that_cannot_be_stored_is_replaced_by_the_model_asked_for(reported):
    model = client(lambda request: reply_naming(reported))
    reply = model.chat("repo_analyzer", [{"role": "user", "content": "hi"}])
    assert reply.model == model.router.resolve("repo_analyzer").model


def test_model_name_the_provider_reports_is_kept():
    model = client(lambda request: reply_naming("provider/routed-model:free"))
    reply = model.chat("repo_analyzer", [{"role": "user", "content": "hi"}])
    assert reply.model == "provider/routed-model:free"


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


# ------------------------------------------------------------- the provider's key
HANDED_IN = "key-handed-in-0123456789"


def key_sent_by(model: RouterChatModel) -> str:
    """The ``Authorization`` header of one chat request of ``model``."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["authorization"])
        return reply_naming("fake/model")

    model.transport = httpx.MockTransport(handler)
    model.chat("writer", [{"role": "user", "content": "x"}])
    (authorization,) = sent
    return authorization


def router_with(environ: dict[str, str]) -> ModelRouter:
    return ModelRouter.from_file(MODELS_YAML, env=environ)


def test_key_handed_to_a_model_is_the_one_sent():
    model = RouterChatModel(router=router_with({}), api_key=HANDED_IN)
    assert key_sent_by(model) == f"Bearer {HANDED_IN}"


def test_key_handed_in_is_sent_instead_of_the_environments():
    router = router_with({"OPENROUTER_API_KEY": "test-key"})
    assert key_sent_by(RouterChatModel(router=router, api_key=HANDED_IN)) == f"Bearer {HANDED_IN}"


def test_key_of_the_environment_is_sent_when_none_is_handed_in():
    router = router_with({"OPENROUTER_API_KEY": "test-key"})
    assert key_sent_by(RouterChatModel(router=router)) == "Bearer test-key"


def test_model_without_a_key_anywhere_names_the_variable_to_set():
    model = RouterChatModel(router=router_with({}))
    with pytest.raises(ConfigError, match="environment variable OPENROUTER_API_KEY is not set"):
        model.chat("writer", [{"role": "user", "content": "x"}])


@pytest.mark.parametrize("environ", [{}, {"OPENROUTER_API_KEY": ""}, {"OPENROUTER_API_KEY": " \n"}])
def test_router_says_when_its_environment_has_no_key(environ):
    assert router_with(environ).api_key_in_environment() is None


def test_router_reads_the_key_without_the_whitespace_around_it():
    router = router_with({"OPENROUTER_API_KEY": " test-key\n"})
    assert router.api_key_in_environment() == "test-key"
    assert router.headers()["Authorization"] == "Bearer test-key"


def test_router_reads_the_key_from_the_variable_its_configuration_names():
    router = router_with({"OPENROUTER_API_KEY": "test-key", "OTHER_KEY": "other-key"})
    router.config.provider.api_key_env = "OTHER_KEY"
    assert router.api_key_in_environment() == "other-key"
    assert router.headers()["Authorization"] == "Bearer other-key"


def test_key_is_in_no_text_of_the_model_or_its_router():
    router = router_with({"OPENROUTER_API_KEY": "test-key"})
    model = RouterChatModel(router=router, api_key=HANDED_IN)
    shown = [repr(model), str(model), repr(router), str(router)]
    assert all(HANDED_IN not in text and "test-key" not in text for text in shown)


def test_failure_of_a_request_does_not_repeat_the_key_handed_in():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not send {request.headers['authorization']}")

    model = RouterChatModel(
        router=router_with({}), transport=httpx.MockTransport(handler), api_key=HANDED_IN
    )
    with pytest.raises(LLMError) as failure:
        model.chat("writer", [{"role": "user", "content": "x"}])
    assert HANDED_IN not in str(failure.value)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_image_part_shows_a_model_the_bytes_as_a_data_url():
    part = image_part(PNG, "image/png")
    assert part == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
    }


@pytest.mark.parametrize(
    ("data", "mime", "reason"),
    [
        (PNG, "image/gif", "PNG, JPEG or WebP"),
        (b"", "image/png", "empty"),
        (PNG, "image/jpeg", "not a image/jpeg image"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_IMAGE_PART_BYTES, "image/png", "at most"),
    ],
    ids=["other-type", "empty", "bytes-of-another-type", "too-large"],
)
def test_image_part_is_refused_for_what_is_not_an_accepted_image(data, mime, reason):
    with pytest.raises(ImagePartRefused, match=reason):
        image_part(data, mime)


def test_vision_role_is_sent_the_image_part_in_its_messages():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "a cat"}}]})

    content = [{"type": "text", "text": "what is this?"}, image_part(PNG, "image/png")]
    client(handler).chat("asset_verifier", [{"role": "user", "content": content}])

    assert seen["body"]["messages"][0]["content"][1]["type"] == "image_url"


# ------------------------------------------------------------ forced tool choice refused
FORCE_T = {"type": "function", "function": {"name": "t"}}
NO_FORCED_CHOICE = {
    "error": {
        "message": "No endpoints found that support the provided 'tool_choice' value. "
        "To learn more about provider routing, visit: https://openrouter.ai/docs",
        "code": 404,
        "metadata": {"routing_funnel": []},
    }
}
CALLED_T = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "{}"}}],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"cost": 0.001},
}


def refusing_forced_choice(sent: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if "tool_choice" in body:
            return httpx.Response(404, json=NO_FORCED_CHOICE)
        return httpx.Response(200, json=CALLED_T)

    return handler


def test_forced_choice_refused_is_asked_again_in_words_without_it():
    sent: list[dict] = []
    model = client(refusing_forced_choice(sent))
    reply = model.chat(
        "repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL], tool_choice=FORCE_T
    )

    assert [("tool_choice" in body) for body in sent] == [True, False]
    again = sent[1]
    assert again["tools"] == [TOOL]
    assert again["messages"] == [
        {"role": "user", "content": "hi"},
        {
            "role": "user",
            "content": "Answer now by calling the tool `t` with your final answer; "
            "do not call any other tool and do not answer in text.",
        },
    ]
    assert reply.tool_calls[0]["function"]["name"] == "t"
    assert model.calls == [reply]


def test_required_choice_refused_is_asked_again_for_one_of_the_tools():
    sent: list[dict] = []
    model = client(refusing_forced_choice(sent))
    model.chat("repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL],
               tool_choice="required")  # fmt: skip

    assert "tool_choice" not in sent[1]
    assert sent[1]["messages"][-1] == {
        "role": "user",
        "content": "Answer now by calling one of the tools with your final answer; "
        "do not answer in text.",
    }


def test_model_that_refused_a_forced_choice_is_not_sent_one_again():
    sent: list[dict] = []
    model = client(refusing_forced_choice(sent))
    for _ in range(2):
        model.chat("repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL],
                   tool_choice=FORCE_T)  # fmt: skip

    assert [("tool_choice" in body) for body in sent] == [True, False, False]


def test_forced_choice_refused_is_logged_with_the_model_only(caplog):
    sent: list[dict] = []
    model = client(refusing_forced_choice(sent))
    with caplog.at_level("INFO", logger="openmarketer_core.llm"):
        model.chat("repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL],
                   tool_choice=FORCE_T)  # fmt: skip

    model_id = model.router.resolve("repo_analyzer").model
    assert f"model {model_id} does not take a forced tool choice" in caplog.text
    assert "routing" not in caplog.text


def test_other_404_of_a_forced_request_is_still_an_error():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(404, json={"error": {"message": "No endpoints found for x/y."}})

    model = client(handler)
    with pytest.raises(LLMError, match="answered HTTP 404") as e:
        model.chat("repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL],
                   tool_choice=FORCE_T)  # fmt: skip
    assert e.value.status == 404
    assert len(sent) == 1
    assert "No endpoints" not in str(e.value)


def test_second_request_refused_too_raises_without_the_providers_words():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=NO_FORCED_CHOICE)

    model = client(handler)
    with pytest.raises(LLMError) as e:
        model.chat("repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL],
                   tool_choice=FORCE_T)  # fmt: skip
    assert str(e.value) == "the model provider answered HTTP 404"


def test_request_that_does_not_force_a_tool_is_not_sent_again_on_that_404():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(404, json=NO_FORCED_CHOICE)

    model = client(handler)
    with pytest.raises(LLMError):
        model.chat("repo_analyzer", [{"role": "user", "content": "hi"}], tools=[TOOL])
    assert len(sent) == 1
