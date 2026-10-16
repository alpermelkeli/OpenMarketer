"""Tests for the chat agent, driven by a scripted model (no network).

The turn is the one the API will run: in memory, on the project's stored records, faked
through the ``ChatReads`` port.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.agent_runtime.langgraph_agent import compile_agent
from openmarketer_core.db.models import (
    AnalysisRunStatus,
    ProfileStatus,
    ProjectStatus,
    ResearchRunStatus,
)
from openmarketer_core.llm import ChatReply
from openmarketer_core.orchestrator.chat_agent.definitions import ChatTurnContext, agent
from openmarketer_core.orchestrator.chat_agent.graph import FORCE_SUBMIT, answer_message
from openmarketer_core.orchestrator.chat_agent.rules import (
    SUBMIT,
    ChatLimits,
    ChatTurnError,
    submit_schema,
)
from openmarketer_core.orchestrator.chat_agent.tools import (
    list_pending_proposals,
    list_research_runs,
    list_runs,
    read_profile,
    read_research_report,
)
from openmarketer_core.orchestrator.reads import (
    MAX_LISTED_RECORDS,
    PROFILE_SECTIONS,
    PendingProposals,
    ProfileReading,
    ProjectStatusReading,
    ProposalReading,
    ResearchReportReading,
    ResearchRunReading,
    RunReading,
    UnknownProfileSection,
)

PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
PROPOSAL_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")
RESEARCH_RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000004")


def call(name: str, **arguments) -> dict:
    return {"id": f"c{id(arguments)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip


class ScriptedModel:
    """Replays prepared assistant turns and records what it was sent.

    A turn that is an exception is raised instead of answered.
    """

    def __init__(self, *turns, cost: float = 0.01):
        self.turns = list(turns)
        self.cost = cost
        self.requests: list[dict] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None):
        self.requests.append(
            {"role": role, "messages": [dict(m) for m in messages], "tool_choice": tool_choice}
        )
        turn = self.turns.pop(0) if self.turns else [call("get_project_status")]
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, str):
            message = {"role": "assistant", "content": turn}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=self.cost)

    def tool_results(self) -> list[str]:
        return [m["content"] for m in self.requests[-1]["messages"] if m["role"] == "tool"]


class FakeReads:
    """The project's records, canned, and what was asked for them.

    ``empty`` leaves out the profile, the runs and the proposals: a project nothing was done
    for yet.
    """

    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.status_calls = 0
        self.profile_calls: list[str | None] = []
        self.proposals_calls = 0
        self.proposal_calls: list[uuid.UUID] = []
        self.run_calls: list[uuid.UUID] = []
        self.run_limits: list[int] = []
        self.research_limits: list[int] = []
        self.report_calls: list[uuid.UUID | None] = []

    def project_status(self) -> ProjectStatusReading:
        self.status_calls += 1
        return ProjectStatusReading(
            project_id=PROJECT_ID,
            name="Memoria",
            repository_url="https://github.com/owner/memoria",
            status=ProjectStatus.ACTIVE,
            autonomy_level=2,
            product_url=None,
            latest_approved_version=None if self.empty else 3,
            latest_draft_version=None,
            unfinished_run_id=None,
            latest_run=None if self.empty else self._run(),
            pending=PendingProposals(
                count=0 if self.empty else 2,
                next_expiry=None if self.empty else datetime.now(UTC),
            ),
        )

    def profile(self, section: str | None = None) -> ProfileReading | None:
        self.profile_calls.append(section)
        if section is not None and section not in PROFILE_SECTIONS:
            raise UnknownProfileSection(section)
        if self.empty:
            return None
        return ProfileReading(
            version=3,
            status=ProfileStatus.APPROVED,
            commit_sha="a" * 40,
            edited_from_version=None,
            created_at=datetime.now(UTC),
            approved_at=datetime.now(UTC),
            content=self._content(section),
        )

    def pending_proposals(self) -> list[ProposalReading]:
        self.proposals_calls += 1
        if self.empty:
            return []
        return [self._proposal()]

    def proposal(self, proposal_id: uuid.UUID) -> ProposalReading | None:
        self.proposal_calls.append(proposal_id)
        if proposal_id != PROPOSAL_ID:
            return None
        return self._proposal()

    def run(self, run_id: uuid.UUID) -> RunReading | None:
        self.run_calls.append(run_id)
        if run_id != RUN_ID:
            return None
        return self._run()

    def recent_runs(self, limit: int) -> list[RunReading]:
        self.run_limits.append(limit)
        return [] if self.empty else [self._run()]

    def recent_research(self, limit: int) -> list[ResearchRunReading]:
        self.research_limits.append(limit)
        return [] if self.empty else [self._research_run()]

    def research_report(self, run_id: uuid.UUID | None = None) -> ResearchReportReading | None:
        self.report_calls.append(run_id)
        if self.empty or (run_id is not None and run_id != RESEARCH_RUN_ID):
            return None
        return self._research_report()

    def _content(self, section: str | None) -> object | None:
        if section is None:
            return None
        if section == "product":
            return {"name": "Memoria", "type": "consumer_app"}
        return {"note": f"the {section} section"}

    def _run(self) -> RunReading:
        now = datetime.now(UTC)
        return RunReading(
            id=RUN_ID,
            status=AnalysisRunStatus.SUCCEEDED,
            error=None,
            profile_version=3,
            created_at=now,
            started_at=now,
            finished_at=now,
        )

    def _proposal(self) -> ProposalReading:
        now = datetime.now(UTC)
        return ProposalReading(
            id=PROPOSAL_ID,
            action_name="publish_post",
            effect=ActionEffect.PUBLISHES,
            payload={"platform": "x"},
            status=ProposalStatus.PENDING,
            run_kind="writer",
            run_id=RUN_ID,
            created_at=now,
            expires_at=now,
            decided_at=None,
        )

    def _research_run(self) -> ResearchRunReading:
        now = datetime.now(UTC)
        return ResearchRunReading(
            id=RESEARCH_RUN_ID,
            status=ResearchRunStatus.SUCCEEDED,
            requested_by="person",
            error=None,
            report_id=PROPOSAL_ID,
            created_at=now,
            started_at=now,
            finished_at=now,
        )

    def _research_report(self) -> ResearchReportReading:
        return ResearchReportReading(
            run_id=RESEARCH_RUN_ID,
            requested_by="person",
            questions=["What did competitors post last month?"],
            findings=[
                {
                    "question_index": 0,
                    "statement": "A competitor shipped a dark mode.",
                    "confidence": 0.8,
                    "flagged": False,
                    "sources": [
                        {"page_id": str(RUN_ID), "url": "https://example.com/a", "quotes": []}
                    ],
                }
            ],
            notes=[],
            cost_usd=0.12,
            model="fake/model",
            created_at=datetime.now(UTC),
        )


@pytest.fixture
def reads() -> FakeReads:
    return FakeReads()


def history() -> list[dict]:
    return [{"role": "user", "content": "How is it going?"}]


LONG_MESSAGE = "x" * 2_000


def long_history() -> list[dict]:
    """A conversation past the compaction threshold, with messages that are not it."""
    history: list[dict] = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"exchange {i}: {LONG_MESSAGE}"}
        for i in range(15)
    ]
    history.append({"role": "tool", "content": "not the conversation"})
    history.append({"role": "user", "content": 42})
    return history


# -------------------------------------------------------------------- loop
def test_answers_after_reading(reads):
    model = ScriptedModel(
        [call("get_project_status")],
        [call(SUBMIT, answer="Two proposals wait.")],
    )
    turn = answer_message(reads, model, history())
    assert turn.answer == "Two proposals wait."
    assert turn.tool_calls == ["get_project_status", SUBMIT]
    assert (turn.steps, turn.model) == (2, "fake/model")
    assert turn.cost_usd == pytest.approx(0.02)
    assert all(r["role"] == "orchestrator" for r in model.requests)


def test_tool_results_reach_the_model(reads):
    model = ScriptedModel(
        [call("get_project_status")],
        [call(SUBMIT, answer="Two proposals wait.")],
    )
    answer_message(reads, model, history())
    reading = json.loads(model.tool_results()[0])
    assert reading["name"] == "Memoria"


def test_first_message_is_the_system_prompt_then_the_conversation(reads):
    model = ScriptedModel([call(SUBMIT, answer="Fine.")])
    answer_message(
        reads,
        model,
        [{"role": "user", "content": "How is it going?"}, {"role": "tool", "content": "x"}],
    )
    messages = model.requests[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("You are the orchestrator")
    assert messages[1] == {"role": "user", "content": "How is it going?"}
    assert len(messages) == 2


def test_text_only_reply_is_nudged_back_to_the_tools(reads):
    model = ScriptedModel("I'll look.", [call(SUBMIT, answer="ok")])
    turn = answer_message(reads, model, history())
    assert turn.answer == "ok"
    assert "call submit_answer" in model.requests[1]["messages"][-1]["content"]


def test_unknown_tool_is_reported_back(reads):
    model = ScriptedModel([call("delete_everything")], [call(SUBMIT, answer="ok")])
    answer_message(reads, model, history())
    result = model.tool_results()[0]
    assert result.startswith("error:")
    assert "is not one of the agent's tools" in result


def test_bad_tool_arguments_are_reported_back(reads):
    model = ScriptedModel([call("get_run", run_id="not-an-id")], [call(SUBMIT, answer="ok")])
    answer_message(reads, model, history())
    result = model.tool_results()[0]
    assert result.startswith("error:")
    assert "run_id" in result
    assert "not-an-id" not in result


def test_unparseable_arguments_are_reported_back(reads):
    broken = {"id": "c1", "type": "function", "function": {"name": "get_run", "arguments": "{"}}
    model = ScriptedModel([broken], [call(SUBMIT, answer="ok")])
    answer_message(reads, model, history())
    assert model.tool_results()[0].startswith("error: could not parse the arguments")


def test_an_empty_answer_is_rejected_then_accepted(reads):
    model = ScriptedModel([call(SUBMIT, answer="")], [call(SUBMIT, answer="Done.")])
    turn = answer_message(reads, model, history())
    assert "submit_answer again" in model.tool_results()[0]
    assert turn.answer == "Done."


# ------------------------------------------------------------------ limits
def test_missing_answer_on_the_last_chance_fails(reads):
    model = ScriptedModel([call(SUBMIT, answer="")])
    with pytest.raises(ChatTurnError, match="does not fit"):
        answer_message(reads, model, history(), limits=ChatLimits(max_model_calls=2))


def test_forced_to_answer_when_the_model_calls_run_out(reads):
    model = ScriptedModel()  # only ever reads the status
    with pytest.raises(ChatTurnError, match="no answer after 4 model calls"):
        answer_message(reads, model, history(), limits=ChatLimits(max_model_calls=4))
    choices = [r["tool_choice"] for r in model.requests]
    assert choices == [None, None, FORCE_SUBMIT, FORCE_SUBMIT]
    wrap_up = [
        m for m in model.requests[-1]["messages"] if "You are out of reading budget" in str(m)
    ]
    assert len(wrap_up) == 1


def test_cost_limit_stops_the_turn(reads):
    model = ScriptedModel(cost=0.2)
    with pytest.raises(ChatTurnError, match="cost limit reached"):
        answer_message(reads, model, history(), limits=ChatLimits(max_cost_usd=0.5))
    assert len(model.requests) == 3
    assert model.requests[-1]["tool_choice"] is not None


# ------------------------------------------------------------------- tools
def test_read_profile_passes_the_section_and_refuses_an_unknown_one(reads):
    model = ScriptedModel(
        [call("read_profile", section="product"), call("read_profile", section="pricing")],
        [call(SUBMIT, answer="ok")],
    )
    answer_message(reads, model, history())
    assert reads.profile_calls == ["product"]
    first, second = model.tool_results()
    assert json.loads(first)["content"]["name"] == "Memoria"
    assert second.startswith("error:")
    assert all(section in second for section in PROFILE_SECTIONS)


def test_list_runs_bounds_the_limit(reads):
    model = ScriptedModel([call("list_runs", limit=100)], [call(SUBMIT, answer="ok")])
    answer_message(reads, model, history())
    assert reads.run_limits == [MAX_LISTED_RECORDS]


def test_list_research_runs_bounds_the_limit(reads):
    model = ScriptedModel([call("list_research_runs", limit=100)], [call(SUBMIT, answer="ok")])
    answer_message(reads, model, history())
    assert reads.research_limits == [MAX_LISTED_RECORDS]


def test_read_research_report_passes_the_run_and_can_default_to_the_latest(reads):
    model = ScriptedModel(
        [call("read_research_report", run_id=str(RESEARCH_RUN_ID))],
        [call("read_research_report")],
        [call(SUBMIT, answer="ok")],
    )
    answer_message(reads, model, history())
    assert reads.report_calls == [RESEARCH_RUN_ID, None]
    first, second = model.tool_results()
    assert json.loads(first)["findings"][0]["statement"].startswith("A competitor")
    assert json.loads(second)["run_id"] == str(RESEARCH_RUN_ID)


def test_read_research_report_refuses_a_non_uuid(reads):
    model = ScriptedModel(
        [call("read_research_report", run_id="not-an-id")], [call(SUBMIT, answer="ok")]
    )
    answer_message(reads, model, history())
    assert reads.report_calls == []
    assert model.tool_results()[0].startswith("error:")


def test_read_research_report_without_a_report_says_so():
    context = ChatTurnContext(
        model=ScriptedModel(), reads=FakeReads(empty=True), limits=ChatLimits()
    )
    assert read_research_report(context) == "No research run of this project has a report yet."
    assert list_research_runs(context) == "The project has no research runs."
    assert read_research_report(context, run_id=str(RESEARCH_RUN_ID)).startswith("error:")


def test_empty_readings_are_plain_sentences():
    context = ChatTurnContext(
        model=ScriptedModel(), reads=FakeReads(empty=True), limits=ChatLimits()
    )
    assert list_pending_proposals(context) == "No proposals wait for a person."
    assert list_runs(context) == "The project has no analysis runs."
    assert read_profile(context) == "The project has no Product Profile yet."


def test_tool_schemas_are_the_reads_plus_the_submission():
    schemas = agent.tool_schemas()
    assert [schema["function"]["name"] for schema in schemas] == [
        "get_project_status",
        "read_profile",
        "list_pending_proposals",
        "explain_proposal",
        "get_run",
        "list_runs",
        "list_research_runs",
        "read_research_report",
    ]
    assert submit_schema()["function"]["name"] == SUBMIT
    by_name = {schema["function"]["name"]: schema for schema in schemas}
    parameters = by_name["read_profile"]["function"]["parameters"]
    assert "section" in parameters["properties"]
    assert not parameters.get("required")
    assert not by_name["get_project_status"]["function"]["parameters"].get("properties")


# ------------------------------------------------------------------- graph
def test_chat_agent_is_a_langgraph_graph():
    from langgraph.graph.state import CompiledStateGraph

    graph = compile_agent(agent, None)
    assert isinstance(graph, CompiledStateGraph)
    assert {"call_model", "run_tools"} <= set(graph.get_graph().nodes)


def test_graph_modules_do_not_depend_on_the_framework():
    import inspect

    from openmarketer_core.orchestrator.chat_agent import definitions, rules, tools

    for module in (definitions, rules, tools):
        assert "import langgraph" not in inspect.getsource(module)
        assert "from langgraph" not in inspect.getsource(module)


# -------------------------------------------------------------- compaction
def test_a_short_history_is_not_summarised(reads):
    model = ScriptedModel([call(SUBMIT, answer="ok")])
    history = [{"role": "user", "content": "hi"} for _ in range(13)]
    turn = answer_message(reads, model, history)
    assert turn.answer == "ok"
    assert len(model.requests) == 1
    assert [m["content"] for m in model.requests[0]["messages"]][1:] == ["hi"] * 13


def test_long_history_is_summarised_before_answering(reads):
    model = ScriptedModel("The operator asked about runs.", [call(SUBMIT, answer="ok")])
    turn = answer_message(reads, model, long_history())
    assert turn.answer == "ok"
    summarising = model.requests[0]["messages"]
    assert summarising[0]["content"].startswith("You condense")
    assert summarising[1]["content"] == "exchange 0: " + LONG_MESSAGE
    assert summarising[-1]["content"] == "Summarise the conversation above."
    answering = model.requests[1]["messages"]
    assert answering[1]["content"].startswith("Summary of the earlier conversation:")
    assert "The operator asked about runs." in answering[1]["content"]


def test_compaction_keeps_the_newest_messages_word_for_word(reads):
    model = ScriptedModel("summary", [call(SUBMIT, answer="ok")])
    answer_message(reads, model, long_history())
    contents = [m["content"] for m in model.requests[1]["messages"]]
    assert len(contents) == 2 + 12
    assert contents[2] == "exchange 3: " + LONG_MESSAGE
    assert contents[-1] == "exchange 14: " + LONG_MESSAGE


def test_compaction_leaves_out_messages_that_are_not_the_conversation(reads):
    model = ScriptedModel("summary", [call(SUBMIT, answer="ok")])
    answer_message(reads, model, long_history())
    summarising = model.requests[0]["messages"]
    assert all(isinstance(m["content"], str) for m in summarising)
    assert "not the conversation" not in [m["content"] for m in summarising]


def test_the_summary_call_counts_against_the_turn(reads):
    model = ScriptedModel("summary", [call(SUBMIT, answer="ok")], cost=0.02)
    turn = answer_message(reads, model, long_history())
    assert turn.cost_usd == pytest.approx(0.04)


def test_an_empty_summary_fails_the_turn(reads):
    model = ScriptedModel("   ", [call(SUBMIT, answer="ok")])
    with pytest.raises(ChatTurnError):
        answer_message(reads, model, long_history())
