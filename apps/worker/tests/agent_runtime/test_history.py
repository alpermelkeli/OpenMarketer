"""Tests for what the runtime lets into the history of a workflow: identifiers, names, small JSON.

The history is stored for as long as the namespace keeps it and is visible in Temporal's UI, so a
secret a handler knew, the context of an error, or a payload of megabytes must not reach it.
They run on a real server with the scripted graph and handlers; ``readable`` decodes the history,
and a value the tests know to be there (``"m-1"``) shows that it does.
"""

import json

import pytest
from scripted_actions import ScriptedAction, nested, routes_of, worded_while_handling
from scripted_graphs import SEND_THEN_PUBLISH, Ask, ScriptedGraph
from scripted_history import exchanges, failures_in, readable
from scripted_rig import ALLOWED, cause_of, some_run
from temporalio.exceptions import ActivityError, RetryState

from openmarketer_worker.agent_runtime import vocabulary
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    MAX_STEP_BYTES,
)

SECRET = "sk-live-4242-not-for-history"
BULK = "BULK" * (MAX_STEP_BYTES // 4 + 1)


async def test_history_holds_only_identifiers_names_and_small_json(runtime, serving):
    send = ScriptedAction([{"message_id": "m-1"}])
    publish = ScriptedAction([RuntimeError(f"the service refused the key {SECRET}")] * 2)
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        await execution.finish(run)
        history = await execution.history(run)
    # A value the history does hold, to show that ``readable`` does show what it holds.
    assert "m-1" in readable(history)
    assert SECRET not in readable(history)
    seen = exchanges(history)
    assert {exchange.activity for exchange in seen} == {"run_graph_step", "run_action"}
    for exchange in seen:
        assert len(json.dumps(exchange.given)) + len(json.dumps(exchange.returned)) < 1000
        if exchange.activity == "run_graph_step":
            assert set(exchange.given) == {"run", "resume"}
            assert set(exchange.given["run"]) == {"kind", "run_id"}
            assert set(exchange.returned) == {"output", "action"}
        else:
            assert set(exchange.given) == {"action_id", "name", "payload"}
    # The action that completed returned an outcome; the one that did not has only its failure.
    outcomes = [e.returned for e in seen if e.activity == "run_action" and e.returned is not None]
    assert [set(outcome) for outcome in outcomes] == [{"action_id", "succeeded", "result", "error"}]


async def test_history_names_no_secret_a_failing_handler_knew(runtime, serving):
    publish = ScriptedAction([RuntimeError(f"the service refused the key {SECRET}")] * 2)
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[1:]), some_run()
    async with serving(runtime(graph, routes_of(ScriptedAction(), publish))) as execution:
        output = await execution.finish(run)
        history = await execution.history(run)
    assert len(publish.calls) == ALLOWED["publish"].max_attempts
    assert output["answers"][0]["error"] == ACTION_TEXTS.stopped_unexpectedly
    assert ACTION_TEXTS.stopped_unexpectedly in readable(history)
    assert SECRET not in readable(history)


# ---------------------------------------------------- what a handler's error may carry in
async def test_history_has_no_context_of_the_error_a_handler_worded_while_handling_another(
    runtime, serving
):
    send = ScriptedAction([worded_while_handling(SECRET)])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        output = await execution.finish(run)
        history = await execution.history(run)
    assert output["answers"][0]["error"] == "the service refused the message"
    assert "the service refused the message" in readable(history)
    assert SECRET not in readable(history)


# ------------------------------------------ what a step may hand over to the history
async def test_step_that_asks_with_a_payload_over_the_limit_ends_the_run_and_history_has_none(
    runtime, serving
):
    send = ScriptedAction()
    graph, run = ScriptedGraph([Ask("send_email", {"to": BULK})]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        failure = await execution.fail(run)
        history = await execution.history(run)
    assert isinstance(failure, ActivityError)
    assert failure.retry_state is RetryState.NON_RETRYABLE_FAILURE
    assert cause_of(failure).type == vocabulary.STEP_REFUSED
    assert graph.advanced == [None]
    assert send.calls == []
    assert str(MAX_STEP_BYTES) in readable(history)
    assert "BULK" not in readable(history)


async def test_step_that_finishes_with_an_output_over_the_limit_ends_the_run_and_history_has_none(
    runtime, serving
):
    graph, run = ScriptedGraph([], note=BULK), some_run()
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        failure = await execution.fail(run)
        history = await execution.history(run)
    assert isinstance(failure, ActivityError)
    assert failure.retry_state is RetryState.NON_RETRYABLE_FAILURE
    assert cause_of(failure).type == vocabulary.STEP_REFUSED
    assert graph.advanced == [None]
    assert str(MAX_STEP_BYTES) in readable(history)
    assert "BULK" not in readable(history)


# ------------------------------------------ what an action's result may hand over
@pytest.mark.parametrize(
    "result",
    [nested(1000), nested(20000), {"x": float("nan")}],
    ids=["depth-1000", "depth-20000", "nan"],
)
async def test_result_the_history_cannot_hold_ends_the_run_and_the_action_ran_once(
    runtime, serving, result
):
    send = ScriptedAction([result, {"message_id": "m-1"}])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        failure = await execution.fail(run)
        history = await execution.history(run)
    assert isinstance(failure, ActivityError)
    assert failure.retry_state is RetryState.NON_RETRYABLE_FAILURE
    assert cause_of(failure).type == vocabulary.INVALID_ACTION_RESULT
    assert len(send.calls) == 1
    assert "NaN" not in readable(history)
    assert '{"x":{"x":{"x":' not in readable(history)
    assert "the result of action send_email is not JSON" in failures_in(history)
