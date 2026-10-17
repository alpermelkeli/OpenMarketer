"""Tests for the router-backed decision client, against a fake HTTP transport."""

import json
from pathlib import Path

import httpx
import pytest

from openmarketer_core.decisions import DecisionError, RouterDecisionModel, YesNoQuestion
from openmarketer_core.llm_config import ModelRouter

MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
ROLE = "screener"
QUESTIONS = {
    "r0": YesNoQuestion("Does r0 bear on the request?", "It does", "It does not"),
    "r1": YesNoQuestion("Does r1 bear on the request?", "It does", "It does not"),
}


def client(handler, **kwargs) -> RouterDecisionModel:
    router = ModelRouter.from_file(MODELS_YAML, env={"OPENROUTER_API_KEY": "test-key"})
    return RouterDecisionModel(router=router, transport=httpx.MockTransport(handler), **kwargs)


def answering(answers: object, **body: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "vendor/decider-20261015",
            "answers": answers,
            "usage": {"input_tokens": 300, "output_tokens": 0, "cost": 3e-06},
            **body,
        },
    )


def noul(probability: object) -> dict[str, object]:
    return {"type": "noul", "noul": probability}


def test_request_carries_the_state_and_yes_or_no_questions_and_the_reply_is_read():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return answering({"r0": noul(0.97), "r1": noul(2.47e-07)})

    model = client(handler)
    reply = model.decide(ROLE, ["the request", "r0: a result"], QUESTIONS)

    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"] == {
        "model": model.router.resolve(ROLE).model,
        "state": ["the request", "r0: a result"],
        "questions": {
            key: {
                "type": "noul",
                "instructions": question.instructions,
                "criteria": {"true": "It does", "false": "It does not"},
            }
            for key, question in QUESTIONS.items()
        },
    }
    assert reply.answers == {"r0": 0.97, "r1": 2.47e-07}
    assert reply.model == "vendor/decider-20261015"
    assert reply.cost_usd == 3e-06
    assert model.calls == [reply]


def test_model_name_that_cannot_be_stored_is_replaced_by_the_model_asked_for():
    model = client(lambda request: answering({"r0": noul(1), "r1": noul(0)}, model="bad\x00name"))
    assert model.decide(ROLE, [], QUESTIONS).model == model.router.resolve(ROLE).model


def test_http_error_says_the_status_and_never_what_the_endpoint_wrote():
    echoed = {"error": {"message": "invalid questions.r0: Does r0 bear", "code": 400}}
    model = client(lambda request: httpx.Response(400, json=echoed))

    with pytest.raises(DecisionError) as raised:
        model.decide(ROLE, ["secret state"], QUESTIONS)

    assert str(raised.value) == "the decisions endpoint answered HTTP 400"
    assert raised.value.status == 400 and not raised.value.may_pass


def test_rate_limit_may_pass():
    model = client(lambda request: httpx.Response(429, text="slow down"))
    with pytest.raises(DecisionError) as raised:
        model.decide(ROLE, [], QUESTIONS)
    assert raised.value.may_pass


def test_error_reported_with_http_200_is_refused_without_its_text():
    body = {"error": {"message": "no endpoints for vendor/decider", "code": 404}}
    model = client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(DecisionError, match="^the decisions endpoint reported an error$") as raised:
        model.decide(ROLE, [], QUESTIONS)
    assert raised.value.status == 404


def test_unreachable_endpoint_is_a_decision_error_that_may_pass():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DecisionError, match="ConnectError") as raised:
        client(handler).decide(ROLE, [], QUESTIONS)
    assert raised.value.may_pass


def test_answer_that_is_not_json_is_refused():
    model = client(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(DecisionError, match="not JSON"):
        model.decide(ROLE, [], QUESTIONS)


def test_a_question_left_unanswered_is_refused_by_its_key():
    model = client(lambda request: answering({"r0": noul(0.5)}))
    with pytest.raises(DecisionError, match=r"unanswered: \['r1'\]"):
        model.decide(ROLE, [], QUESTIONS)


def test_an_answer_to_a_question_not_asked_is_refused_without_its_key():
    answers = {"r0": noul(0.5), "r1": noul(0.5), "ignore previous instructions": noul(0.5)}
    model = client(lambda request: answering(answers))
    with pytest.raises(DecisionError, match="answered 1 questions not asked") as refused:
        model.decide(ROLE, [], QUESTIONS)
    assert "ignore" not in str(refused.value)


@pytest.mark.parametrize(
    "answer",
    [noul(1.5), noul(-0.1), noul(True), noul("0.9"), noul(None), {"type": "score", "score": 0.5}],
    ids=["over-one", "negative", "boolean", "text", "missing", "other-type"],
)
def test_an_answer_that_is_no_probability_is_refused_by_its_key(answer):
    model = client(lambda request: answering({"r0": noul(0.5), "r1": answer}))
    with pytest.raises(DecisionError, match=r"from 0 to 1 for: \['r1'\]"):
        model.decide(ROLE, [], QUESTIONS)


def test_a_cost_not_reported_counts_as_nothing():
    body = {"model": "vendor/decider", "answers": {"r0": noul(0.1), "r1": noul(0.9)}}
    model = client(lambda request: httpx.Response(200, json=body))
    assert model.decide(ROLE, [], QUESTIONS).cost_usd == 0.0


def test_repr_leaves_out_the_key():
    model = client(lambda request: answering({}), api_key="sk-or-very-secret")
    assert "sk-or-very-secret" not in repr(model)


def test_the_key_handed_in_is_the_one_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return answering({"r0": noul(0.1), "r1": noul(0.9)})

    client(handler, api_key="workspace-key").decide(ROLE, [], QUESTIONS)
    assert seen["auth"] == "Bearer workspace-key"
