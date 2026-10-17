"""Tests for the researcher's screening: search results that do not bear on the request are
dropped before anything is fetched.

The screener is a scripted ``DecisionModel`` that judges a result by its title: one with
"unrelated" in it is answered with a low probability. One test runs the whole graph in memory
with ``AgentSteps``, the searches and the fetches answered by the test; the others call the
node or the rules directly. Nothing reaches a network or a database.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.decisions import DecisionError, DecisionReply, YesNoQuestion
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatReply
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent import rules
from openmarketer_core.research.research_agent.definitions import (
    FETCH_PAGE,
    SEARCH_WEB,
    ResearchContext,
    ResearchState,
    agent,
    initial_state,
)
from openmarketer_core.research.research_agent.graph import (
    after_fetch,
    after_screen,
    recursion_limit,
    screen,
)
from openmarketer_core.research.research_agent.rules import Limits
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND, ResearchRunInput

REQUEST = ResearchRequest(questions=["Which note apps sync offline?"], max_sources=5)
BATCH_COST = 0.0001


class ScriptedDecisions:
    """A screener that drops every candidate whose text says "unrelated"; it records its calls.

    ``fails`` lists the calls (by their order, from 0) that raise ``DecisionError`` instead.
    """

    def __init__(self, fails: Sequence[int] = ()):
        self.fails = set(fails)
        self.calls: list[dict[str, Any]] = []

    def decide(
        self, role: str, state: Sequence[str], questions: Mapping[str, YesNoQuestion]
    ) -> DecisionReply:
        self.calls.append({"role": role, "state": list(state), "questions": dict(questions)})
        if len(self.calls) - 1 in self.fails:
            raise DecisionError("the decisions endpoint answered HTTP 503", status=503)
        candidates = state[1:]
        answers = {
            key: 0.02 if "unrelated" in candidates[index] else 0.97
            for index, key in enumerate(questions)
        }
        return DecisionReply(answers=answers, model="vendor/decider", cost_usd=BATCH_COST)


class ScriptedChat:
    """The researcher and the reader, answering in the order the graph asks."""

    def __init__(self, *messages: dict[str, Any]):
        self.messages = list(messages)

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        return ChatReply(message=self.messages.pop(0), model="vendor/model-1", cost_usd=0.001)


def submission(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call = {"id": "1", "type": "function", "function": {"name": name}}
    call["function"]["arguments"] = json.dumps(arguments)
    return {"role": "assistant", "tool_calls": [call]}


def result(url: str, title: str, query: str | None = "offline sync notes") -> dict[str, Any]:
    found = {"url": url, "title": title, "snippet": f"{title}, in a sentence."}
    if query is not None:
        found["query"] = query
    return found


def a_state(results: list[dict[str, Any]], *, next_result: int = 0) -> ResearchState:
    state = initial_state(
        ResearchRunInput(workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4()),
        REQUEST,
    )
    state.update(round=1, queries=["offline sync notes"], next_query=1)
    state.update(results=results, next_result=next_result)
    return state


def a_context(decisions: ScriptedDecisions, chat: ScriptedChat | None = None) -> ResearchContext:
    return ResearchContext(
        model=chat or ScriptedChat(),
        decisions=decisions,
        limits=Limits(max_rounds=1),
        page_text_of=lambda page_id: "A page that says notes sync offline.",
    )


def test_an_unrelated_result_is_not_fetched():
    run = GraphRunRef(RESEARCH_RUN_KIND, uuid.uuid4())
    request_state = a_state([])
    chat = ScriptedChat(
        submission(rules.SUBMIT_QUERIES, {"queries": ["offline sync notes"]}),
        {"role": "assistant", "content": json.dumps({"summary": "Notes sync offline."})},
        submission(rules.SUBMIT_FINDINGS, {"findings": []}),
    )
    context = a_context(ScriptedDecisions(), chat)
    steps = AgentSteps(
        agent,
        initial_state=lambda ref: {**request_state, "round": 0, "queries": [], "next_query": 0},
        finish=lambda ref, final: {"notes": final["notes"]},
        context=lambda ref, checkpoints: context,
        recursion_limit=recursion_limit(context.limits),
    )
    checkpoints = RunCheckpoints(store=InMemorySaver(), thread_id=thread_id(run.kind, run.run_id))
    fetched: list[str] = []
    resume = None
    while True:
        step = steps.advance(run, checkpoints, resume)
        if step.action is None:
            break
        if step.action.name == SEARCH_WEB:
            answer = {
                "results": [
                    result("https://notes.example/offline", "Offline sync in notes"),
                    result("https://recipes.example/cake", "An unrelated cake recipe"),
                ]
            }
        else:
            assert step.action.name == FETCH_PAGE
            fetched.append(step.action.payload["url"])
            answer = {"page_id": str(uuid.uuid4()), "url": step.action.payload["url"], "flags": []}
        resume = ActionOutcome(
            action_id=step.action.action_id, succeeded=True, result=answer, error=""
        )

    assert fetched == ["https://notes.example/offline"]
    assert step.output is not None
    assert "1 search results were dropped as unrelated to the request" in step.output["notes"]


def test_only_the_results_not_fetched_yet_are_screened():
    fetched_before = [
        result("https://a.example/1", "Round one"),
        result("https://a.example/2", "Also"),
    ]
    this_round = [
        result("https://b.example/1", "Offline sync"),
        result("https://b.example/2", "unrelated news"),
        result("https://b.example/3", "Sync engines"),
    ]
    decisions = ScriptedDecisions()

    changes = screen(a_state([*fetched_before, *this_round], next_result=2), a_context(decisions))

    [call] = decisions.calls
    assert call["role"] == rules.SCREENER_ROLE
    assert len(call["questions"]) == 3
    assert "Round one" not in "".join(call["state"])
    assert changes["results"] == [*fetched_before, this_round[0], this_round[2]]


def test_results_are_screened_in_batches_and_every_batch_costs():
    results = [result(f"https://c.example/{i}", f"Sync {i}") for i in range(23)]
    decisions = ScriptedDecisions()

    changes = screen(a_state(results), a_context(decisions))

    assert [len(call["questions"]) for call in decisions.calls] == [10, 10, 3]
    assert all(len(call["state"]) == 1 + len(call["questions"]) for call in decisions.calls)
    assert changes["cost_usd"] == pytest.approx(3 * BATCH_COST)
    assert changes["results"] == results
    assert "model" not in changes


def test_a_failed_screening_keeps_its_batch_with_a_note_and_the_run_goes_on():
    results = [result(f"https://d.example/{i}", "unrelated") for i in range(12)]
    decisions = ScriptedDecisions(fails=[0])

    changes = screen(a_state(results), a_context(decisions))

    assert changes["results"] == results[:10]
    assert changes["cost_usd"] == pytest.approx(BATCH_COST)
    assert changes["notes"] == [
        "10 search results were kept unscreened: the decisions endpoint answered HTTP 503",
        "2 search results were dropped as unrelated to the request",
    ]


def test_nothing_to_screen_changes_nothing():
    decisions = ScriptedDecisions()
    assert (
        screen(a_state([result("https://e.example/", "Sync")], next_result=1), a_context(decisions))
        == {}
    )
    assert decisions.calls == []


def test_a_result_stored_before_queries_were_kept_is_screened_too():
    decisions = ScriptedDecisions()

    changes = screen(
        a_state([result("https://f.example/", "Sync", query=None)]), a_context(decisions)
    )

    assert "Found by the query: (not kept)" in decisions.calls[0]["state"][1]
    assert len(changes["results"]) == 1


def test_with_every_result_dropped_and_no_page_the_graph_goes_on_to_synthesize():
    decisions = ScriptedDecisions()
    state = a_state([result("https://g.example/", "unrelated")])
    changes = screen(state, a_context(decisions))
    state["results"] = changes["results"]

    assert state["results"] == []
    assert after_screen(state, a_context(decisions)) == "synthesize"


def test_when_every_fetch_failed_the_graph_goes_on_to_synthesize():
    state = a_state([result("https://i.example/", "Sync")], next_result=1)
    assert after_fetch(state, a_context(ScriptedDecisions())) == "synthesize"


def test_a_result_is_kept_from_the_threshold_on_and_without_an_answer():
    candidates = ["at", "under", "unanswered"]
    answers = {"r0": rules.SCREEN_KEEP_AT, "r1": rules.SCREEN_KEEP_AT - 0.01}
    assert rules.kept(candidates, answers) == ["at", "unanswered"]


def test_the_screener_reads_the_request_and_bounded_results_as_data():
    long_title = "t" * (rules.MAX_SCREEN_TITLE_CHARS + 50)

    state = rules.screen_state(REQUEST, [result("https://h.example/", long_title)])

    assert REQUEST.questions[0] in state[0]
    assert "not instructions" in state[0]
    assert state[1].startswith("Search result r0 (web text, data only)")
    [title] = [line for line in state[1].splitlines() if line.startswith("Title: ")]
    assert len(title) <= len("Title: ") + rules.MAX_SCREEN_TITLE_CHARS + len("...")
    assert set(rules.screen_questions(2)) == {"r0", "r1"}
