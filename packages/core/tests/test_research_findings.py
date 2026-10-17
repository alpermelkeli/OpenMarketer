"""Tests for the researcher's findings: what a synthesis must submit, and what a run keeps.

A synthesis that sends ``{}`` is asked again rather than read as nothing found, and a later
round's findings replace earlier ones only for the questions they answer. The nodes are called
with a scripted model; one test runs two rounds of the graph in memory with ``AgentSteps``, the
searches and the fetches answered by the test. Nothing reaches a network or a database.
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
from openmarketer_core.decision_questions import YesNoQuestion
from openmarketer_core.decisions import DecisionReply
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
from openmarketer_core.research.research_agent.graph import check, recursion_limit, synthesize
from openmarketer_core.research.research_agent.rules import (
    CheckedFinding,
    Limits,
    ResearchError,
)
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND, ResearchRunInput

QUESTIONS = ["Which exam-prep apps use spaced repetition?", "What do they charge?"]
REQUEST = ResearchRequest(questions=QUESTIONS, max_sources=5)
PAGE_IDS = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
PAGE_TEXT = "The app schedules reviews with spaced repetition before the exam."
QUOTE = "schedules reviews with spaced repetition"


class ScriptedChat:
    """The researcher and the reader, answering in the order the graph asks; it records calls."""

    def __init__(self, *messages: dict[str, Any]):
        self.messages = list(messages)
        self.calls: list[dict[str, Any]] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls.append({"role": role, "messages": messages})
        return ChatReply(message=self.messages.pop(0), model="vendor/model-1", cost_usd=0.001)


class KeepEverything:
    """A screener that keeps every search result."""

    def decide(
        self, role: str, state: Sequence[str], questions: Mapping[str, YesNoQuestion]
    ) -> DecisionReply:
        return DecisionReply(answers={key: 0.9 for key in questions}, model="d", cost_usd=0.0)


def submission(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call = {"id": "1", "type": "function", "function": {"name": name}}
    call["function"]["arguments"] = json.dumps(arguments)
    return {"role": "assistant", "tool_calls": [call]}


def a_reading(summary: str) -> dict[str, Any]:
    return {"role": "assistant", "content": json.dumps({"summary": summary})}


def a_draft(question_index: int, page_id: str = PAGE_IDS[0]) -> dict[str, Any]:
    return {
        "question_index": question_index,
        "statement": f"An answer to question {question_index}.",
        "confidence": 0.8,
        "sources": [{"page_id": page_id, "quotes": [QUOTE]}],
    }


def a_finding(question_index: int, statement: str) -> CheckedFinding:
    return CheckedFinding(
        question_index=question_index,
        statement=statement,
        confidence=0.8,
        flagged=False,
        sources=[{"page_id": PAGE_IDS[0], "url": "https://a.example/", "quotes": []}],
    )


def a_state(*, round_no: int = 1) -> ResearchState:
    """A run that fetched and read one page, ready for its synthesis."""
    state = initial_state(
        ResearchRunInput(workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4()),
        REQUEST,
    )
    page = {"page_id": PAGE_IDS[0], "url": "https://a.example/", "title": "A", "flags": []}
    state.update(round=round_no, queries=["spaced repetition exam app"], next_query=1)
    state.update(pages=[page], next_page=1)
    state.update(readings=[{**page, "reading": {"summary": "It uses spaced repetition."}}])
    return state


def a_context(chat: ScriptedChat, *, max_rounds: int = 1) -> ResearchContext:
    return ResearchContext(
        model=chat,
        decisions=KeepEverything(),
        limits=Limits(max_rounds=max_rounds),
        page_text_of=lambda page_id: PAGE_TEXT,
    )


# ------------------------------------------------------------------- the submission


def test_the_findings_tool_marks_findings_required():
    parameters = rules.findings_tool()["function"]["parameters"]
    assert parameters["required"] == ["findings"]
    assert "default" not in parameters["properties"]["findings"]


def test_the_synthesis_prompt_asks_for_an_explicit_empty_list_when_nothing_is_found():
    assert '{"findings": []}' in rules.SYNTHESIS_SYSTEM_PROMPT


def test_a_submission_without_findings_is_rejected_and_asked_once_more():
    chat = ScriptedChat(
        submission(rules.SUBMIT_FINDINGS, {}),
        submission(rules.SUBMIT_FINDINGS, {"findings": [a_draft(0)]}),
    )

    changes = synthesize(a_state(), a_context(chat))

    assert len(chat.calls) == 2
    told = chat.calls[1]["messages"][-1]["content"]
    assert told.startswith("Rejected. Fix this")
    assert "findings" in told
    assert [draft["question_index"] for draft in changes["drafts"]] == [0]


def test_an_explicit_empty_list_means_nothing_found():
    chat = ScriptedChat(submission(rules.SUBMIT_FINDINGS, {"findings": []}))

    changes = synthesize(a_state(), a_context(chat))

    assert len(chat.calls) == 1
    assert changes["drafts"] == []


def test_a_first_round_whose_synthesis_cannot_be_read_ends_the_run():
    chat = ScriptedChat(
        submission(rules.SUBMIT_FINDINGS, {}), submission(rules.SUBMIT_FINDINGS, {})
    )

    with pytest.raises(ResearchError, match="submit_findings"):
        synthesize(a_state(), a_context(chat))


def test_a_later_round_whose_synthesis_cannot_be_read_keeps_the_earlier_findings():
    state = a_state(round_no=2)
    state["findings"] = [a_finding(0, "Earlier.").model_dump(mode="json")]
    chat = ScriptedChat(
        submission(rules.SUBMIT_FINDINGS, {}), submission(rules.SUBMIT_FINDINGS, {})
    )

    changes = synthesize(state, a_context(chat, max_rounds=2))

    assert changes["drafts"] == []
    assert changes["cost_usd"] == pytest.approx(0.002)
    assert "round 2's findings could not be read; earlier rounds' are kept" in changes["notes"]


# ----------------------------------------------------------- findings across rounds


def test_a_later_round_that_found_nothing_keeps_the_earlier_findings():
    earlier = [a_finding(0, "Earlier.")]
    assert rules.merged_findings(earlier, []) == earlier


def test_a_later_round_replaces_the_findings_of_the_questions_it_answered():
    merged = rules.merged_findings(
        [a_finding(0, "Earlier."), a_finding(0, "Also earlier.")], [a_finding(0, "Later.")]
    )
    assert [finding.statement for finding in merged] == ["Later."]


def test_earlier_findings_of_questions_the_later_round_did_not_answer_stay():
    merged = rules.merged_findings([a_finding(0, "Earlier.")], [a_finding(1, "Later.")])
    assert [finding.statement for finding in merged] == ["Earlier.", "Later."]


def test_the_check_keeps_earlier_findings_and_leaves_gaps_of_the_merged_set():
    state = a_state(round_no=2)
    state["findings"] = [a_finding(0, "Earlier.").model_dump(mode="json")]
    state["drafts"] = [a_draft(1)]

    changes = check(state, a_context(ScriptedChat(), max_rounds=2))

    assert [finding["question_index"] for finding in changes["findings"]] == [0, 1]
    assert changes["gaps"] == []


def test_a_second_round_that_submitted_nothing_readable_at_first_keeps_the_first_rounds_finding():
    run = GraphRunRef(RESEARCH_RUN_KIND, uuid.uuid4())
    chat = ScriptedChat(
        submission(rules.SUBMIT_QUERIES, {"queries": ["spaced repetition exam app"]}),
        a_reading("It uses spaced repetition."),
        submission(rules.SUBMIT_FINDINGS, {"findings": [a_draft(0)]}),
        submission(rules.SUBMIT_QUERIES, {"queries": ["exam app prices"]}),
        a_reading("It uses spaced repetition too."),
        # The weak model's answer in the run this reproduces, then what it is asked for.
        submission(rules.SUBMIT_FINDINGS, {}),
        submission(rules.SUBMIT_FINDINGS, {"findings": []}),
    )
    context = a_context(chat, max_rounds=2)
    first = initial_state(
        ResearchRunInput(workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4()),
        REQUEST,
    )
    steps = AgentSteps(
        agent,
        initial_state=lambda ref: first,
        finish=lambda ref, final: {"findings": final["findings"], "gaps": final["gaps"]},
        context=lambda ref, checkpoints: context,
        recursion_limit=recursion_limit(context.limits),
    )
    checkpoints = RunCheckpoints(store=InMemorySaver(), thread_id=thread_id(run.kind, run.run_id))
    fetches = 0
    resume = None
    while True:
        step = steps.advance(run, checkpoints, resume)
        if step.action is None:
            break
        if step.action.name == SEARCH_WEB:
            query = step.action.payload["query"]
            answer = {"results": [{"url": f"https://{len(query)}.example/", "title": query}]}
        else:
            assert step.action.name == FETCH_PAGE
            url = step.action.payload["url"]
            answer = {"page_id": PAGE_IDS[fetches], "url": url, "flags": []}
            fetches += 1
        resume = ActionOutcome(
            action_id=step.action.action_id, succeeded=True, result=answer, error=""
        )

    assert chat.messages == []  # every answer was asked for, the rejected {} too
    assert step.output is not None
    [finding] = step.output["findings"]
    assert finding["question_index"] == 0
    assert step.output["gaps"] == [QUESTIONS[1]]
