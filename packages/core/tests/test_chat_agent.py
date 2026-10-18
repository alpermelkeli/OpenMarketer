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
    MediaAssetStatus,
    ProfileStatus,
    ProjectStatus,
    ResearchRunStatus,
    StudioRunStatus,
)
from openmarketer_core.llm import ChatReply
from openmarketer_core.orchestrator.chat_agent.definitions import ChatTurnContext, agent
from openmarketer_core.orchestrator.chat_agent.graph import answer_message
from openmarketer_core.orchestrator.chat_agent.rules import (
    SUBMIT,
    SYSTEM_PROMPT,
    TOOL_LABELS,
    UNKNOWN_TOOL,
    ChatLimits,
    ChatTurnError,
    follow_up_instruction,
    is_wrapping_up,
    submit_schema,
    wrap_up_tool_schemas,
)
from openmarketer_core.orchestrator.chat_agent.tools import (
    get_studio_run,
    list_analysis_runs,
    list_media_assets,
    list_pending_proposals,
    list_research_runs,
    list_studio_runs,
    read_profile,
    read_research_report,
    start_creative,
    start_research,
)
from openmarketer_core.orchestrator.reads import (
    MAX_LISTED_RECORDS,
    PROFILE_SECTIONS,
    AssetReviewReading,
    MediaAssetReading,
    PendingProposals,
    ProfileReading,
    ProjectStatusReading,
    ProposalReading,
    ResearchReportReading,
    ResearchRunReading,
    RunReading,
    StudioRunReading,
    UnknownProfileSection,
)
from openmarketer_core.orchestrator.requests import ResearchNotStarted, StudioNotStarted
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.run_events import RUN_STARTED, TOOL_ANSWERED, TOOL_CALLED, RunEvent
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND

PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
PROPOSAL_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")
RESEARCH_RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000004")
STUDIO_RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000005")
ASSET_ID = uuid.UUID("00000000-0000-4000-8000-000000000006")


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
            {
                "role": role,
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "tool_choice": tool_choice,
            }
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
        self.studio_limits: list[int] = []
        self.studio_calls: list[uuid.UUID] = []
        self.asset_limits: list[int] = []

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

    def recent_studio_runs(self, limit: int) -> list[StudioRunReading]:
        self.studio_limits.append(limit)
        return [] if self.empty else [studio_run_reading()]

    def studio_run(self, run_id: uuid.UUID) -> StudioRunReading | None:
        self.studio_calls.append(run_id)
        if self.empty or run_id != STUDIO_RUN_ID:
            return None
        review = AssetReviewReading(
            version_id=RUN_ID,
            passed=False,
            check_failures=["copy"],
            failed_criteria=[{"criterion": "legibility", "severity": "major", "note": "small"}],
        )
        return studio_run_reading(
            StudioRunStatus.NEEDS_REVIEW, review=review, review_reason="the round limit"
        )

    def media_assets(self, limit: int) -> list[MediaAssetReading]:
        self.asset_limits.append(limit)
        if self.empty:
            return []
        return [
            MediaAssetReading(
                id=ASSET_ID,
                run_id=STUDIO_RUN_ID,
                platform="x_post",
                status=MediaAssetStatus.DRAFT,
                current_version_id=RUN_ID,
                created_at=datetime.now(UTC),
                decided_at=None,
            )
        ]

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


def studio_run_reading(
    status: StudioRunStatus = StudioRunStatus.SUCCEEDED,
    *,
    review: AssetReviewReading | None = None,
    review_reason: str | None = None,
) -> StudioRunReading:
    now = datetime.now(UTC)
    queued = status is StudioRunStatus.QUEUED
    return StudioRunReading(
        id=STUDIO_RUN_ID,
        status=status,
        platform="x_post",
        goal="Announce the Android launch",
        exact_copy=["Now on Android"],
        requested_by="orchestrator",
        error=None,
        final_asset_id=None if queued else ASSET_ID,
        cost_usd=0.0 if queued else 0.08,
        created_at=now,
        started_at=None if queued else now,
        finished_at=None if queued else now,
        review=review,
        review_reason=review_reason,
    )


@pytest.fixture
def reads() -> FakeReads:
    return FakeReads()


class FakeRequests:
    """The turn's caller, recorded: what it was asked to start, and what it answers.

    ``fails`` makes it refuse the way the port may (``ResearchNotStarted``); the request is
    recorded either way, so a test can tell a refusal that reached the caller from one that
    never did.
    """

    def __init__(self, *, fails: Exception | None = None) -> None:
        self.fails = fails
        self.started: list[ResearchRequest] = []
        self.creatives: list[AssetRequest] = []

    def start_research(self, request: ResearchRequest) -> ResearchRunReading:
        self.started.append(request)
        if self.fails is not None:
            raise self.fails
        now = datetime.now(UTC)
        return ResearchRunReading(
            id=RESEARCH_RUN_ID,
            status=ResearchRunStatus.QUEUED,
            requested_by="orchestrator",
            error=None,
            report_id=None,
            created_at=now,
            started_at=None,
            finished_at=None,
        )

    def start_creative(self, request: AssetRequest) -> StudioRunReading:
        self.creatives.append(request)
        if self.fails is not None:
            raise self.fails
        return studio_run_reading(StudioRunStatus.QUEUED)


def a_context(reads: FakeReads, requests: FakeRequests | None) -> ChatTurnContext:
    return ChatTurnContext(
        model=ScriptedModel(), reads=reads, limits=ChatLimits(), requests=requests
    )


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
    model = ScriptedModel(
        [call("get_analysis_run", run_id="not-an-id")], [call(SUBMIT, answer="ok")]
    )
    answer_message(reads, model, history())
    result = model.tool_results()[0]
    assert result.startswith("error:")
    assert "run_id" in result
    assert "not-an-id" not in result


def test_unparseable_arguments_are_reported_back(reads):
    broken = {
        "id": "c1",
        "type": "function",
        "function": {"name": "get_analysis_run", "arguments": "{"},
    }
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


def test_the_turn_fails_when_the_model_never_answers(reads):
    model = ScriptedModel()  # only ever reads the status
    with pytest.raises(ChatTurnError, match="no answer after 4 model calls"):
        answer_message(
            reads, model, history(), limits=ChatLimits(max_model_calls=4), requests=FakeRequests()
        )
    reading = [schema["function"]["name"] for schema in model.requests[0]["tools"]]
    wrap_up = [schema["function"]["name"] for schema in model.requests[-1]["tools"]]
    assert "read_profile" in reading
    assert wrap_up == ["start_research", "start_creative", SUBMIT]
    messages = [
        m for m in model.requests[-1]["messages"] if "You are out of reading budget" in str(m)
    ]
    assert len(messages) == 1


def test_request_actions_do_not_count_against_the_reading_budget():
    limits = ChatLimits()
    assert not is_wrapping_up(1, 0.0, ["start_research"] * limits.max_tool_calls, limits)
    assert not is_wrapping_up(1, 0.0, ["start_creative"] * limits.max_tool_calls, limits)
    assert not is_wrapping_up(1, 0.0, ["get_project_status"] * 5, limits)
    assert is_wrapping_up(1, 0.0, ["get_project_status"] * 6, limits)


def test_cost_limit_stops_the_turn(reads):
    model = ScriptedModel(cost=0.2)
    with pytest.raises(ChatTurnError, match="cost limit reached"):
        answer_message(
            reads, model, history(), limits=ChatLimits(max_cost_usd=0.5), requests=FakeRequests()
        )
    assert len(model.requests) == 3
    wrap_up = [schema["function"]["name"] for schema in model.requests[-1]["tools"]]
    assert wrap_up == ["start_research", "start_creative", SUBMIT]


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


def test_list_analysis_runs_bounds_the_limit(reads):
    model = ScriptedModel([call("list_analysis_runs", limit=100)], [call(SUBMIT, answer="ok")])
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


# ---------------------------------------------------------- the request action
def test_start_research_asks_the_caller_for_the_exact_request():
    requests = FakeRequests()
    context = a_context(FakeReads(), requests)

    answer = start_research(
        context,
        questions=["What did competitors post last month?"],
        topics=["social"],
        competitors=["Acme"],
        time_window="last month",
        max_sources=4,
    )

    [request] = requests.started
    assert request == ResearchRequest(
        questions=["What did competitors post last month?"],
        topics=["social"],
        competitors=["Acme"],
        time_window="last month",
        max_sources=4,
    )
    body = json.loads(answer)
    assert body["run_id"] == str(RESEARCH_RUN_ID)
    assert body["status"] == "queued"


def test_start_research_reads_only_the_questions_the_rest_have_defaults():
    requests = FakeRequests()
    context = a_context(FakeReads(), requests)

    start_research(context, questions=["Is it live?"])

    [request] = requests.started
    assert (request.topics, request.competitors, request.time_window) == ([], [], "")
    assert request.max_sources == 8


@pytest.mark.parametrize(
    "arguments",
    [
        {"questions": []},
        {"questions": ["x" * 400]},
        {"questions": ["ok"], "topics": ["t"] * 6},
        {"questions": ["ok"], "time_window": "w" * 41},
        {"questions": ["ok"], "max_sources": 0},
        {"questions": ["ok"], "max_sources": 13},
    ],
    ids=["no-questions", "question-too-long", "too-many-topics", "window-too-long", "none", "many"],
)
def test_start_research_refuses_what_does_not_fit_without_reaching_the_caller(arguments):
    requests = FakeRequests()
    context = a_context(FakeReads(), requests)

    answer = start_research(context, **arguments)

    assert answer.startswith("error:")
    assert requests.started == []
    # The refusal names the fields at fault and never a value the model wrote.
    assert "x" * 10 not in answer
    assert "w" * 10 not in answer


def test_start_research_without_a_caller_says_starting_runs_is_not_available():
    context = a_context(FakeReads(), None)

    assert start_research(context, questions=["Is it live?"]) == (
        "error: starting a research run is not available here"
    )


def test_a_run_that_could_not_be_started_is_an_error_the_turn_reads_not_a_failed_turn():
    refused = ResearchNotStarted("the research could not be handed to a worker")
    context = a_context(FakeReads(), FakeRequests(fails=refused))
    model = ScriptedModel(
        [call("start_research", questions=["Is it live?"])],
        [call(SUBMIT, answer="I could not start it.")],
    )

    turn = answer_message(FakeReads(), model, history(), requests=context.requests)

    assert turn.answer == "I could not start it."
    assert model.tool_results()[0] == ("error: the research could not be handed to a worker")


def test_a_turn_can_start_a_research_run_and_says_its_id(reads):
    requests = FakeRequests()
    model = ScriptedModel(
        [call("start_research", questions=["Is it live?"])],
        [call(SUBMIT, answer=f"Started run {RESEARCH_RUN_ID}.")],
    )

    turn = answer_message(reads, model, history(), requests=requests)

    [request] = requests.started
    assert request.questions == ["Is it live?"]
    assert turn.answer == f"Started run {RESEARCH_RUN_ID}."
    assert json.loads(model.tool_results()[0])["run_id"] == str(RESEARCH_RUN_ID)


def test_a_spent_reading_budget_still_lets_the_turn_start_research(reads):
    requests = FakeRequests()
    model = ScriptedModel(
        [call("get_project_status") for _ in range(6)],
        [call("start_research", questions=["Is it live?"])],
        [call(SUBMIT, answer="Started it.")],
    )

    turn = answer_message(reads, model, history(), requests=requests)

    assert [request.questions for request in requests.started] == [["Is it live?"]]
    assert turn.answer == "Started it."
    assert any("out of reading budget" in str(m) for m in model.requests[1]["messages"])
    wrap_up = [schema["function"]["name"] for schema in model.requests[1]["tools"]]
    assert wrap_up == ["start_research", "start_creative", SUBMIT]


def test_empty_readings_are_plain_sentences():
    context = ChatTurnContext(
        model=ScriptedModel(), reads=FakeReads(empty=True), limits=ChatLimits()
    )
    assert list_pending_proposals(context) == "No proposals wait for a person."
    assert list_analysis_runs(context) == "The project has no analysis runs."
    assert read_profile(context) == "The project has no Product Profile yet."


def test_tool_schemas_are_the_reads_the_request_action_and_the_submission():
    schemas = agent.tool_schemas()
    assert [schema["function"]["name"] for schema in schemas] == [
        "get_project_status",
        "read_profile",
        "list_pending_proposals",
        "explain_proposal",
        "get_analysis_run",
        "list_analysis_runs",
        "list_research_runs",
        "read_research_report",
        "start_research",
        "list_studio_runs",
        "get_studio_run",
        "list_media_assets",
        "start_creative",
    ]
    assert submit_schema()["function"]["name"] == SUBMIT
    by_name = {schema["function"]["name"]: schema for schema in schemas}
    parameters = by_name["read_profile"]["function"]["parameters"]
    assert "section" in parameters["properties"]
    assert not parameters.get("required")
    assert not by_name["get_project_status"]["function"]["parameters"].get("properties")
    started = by_name["start_research"]["function"]["parameters"]
    assert list(started["properties"]) == [
        "questions",
        "topics",
        "competitors",
        "time_window",
        "max_sources",
    ]
    assert started["required"] == ["questions"]
    creative = by_name["start_creative"]["function"]["parameters"]
    assert list(creative["properties"]) == [
        "goal",
        "platform",
        "exact_copy",
        "style_notes",
        "reference_asset_ids",
    ]
    assert creative["required"] == ["goal", "platform"]


def test_the_wrap_up_offers_the_request_action_and_the_submission_only():
    names = [schema["function"]["name"] for schema in wrap_up_tool_schemas(agent)]
    assert names == ["start_research", "start_creative", SUBMIT]


# ------------------------------------------------------------ the creative studio
def test_start_creative_asks_the_caller_for_the_copy_word_for_word():
    requests = FakeRequests()
    context = a_context(FakeReads(), requests)

    answer = start_creative(
        context,
        goal="Announce the Android launch",
        platform="instagram_story",
        exact_copy=["Memoria is on Android", "Get it today"],
        style_notes="dark background",
        reference_asset_ids=[str(ASSET_ID)],
    )

    [request] = requests.creatives
    assert request == AssetRequest(
        goal="Announce the Android launch",
        platform=Platform.INSTAGRAM_STORY,
        exact_copy=["Memoria is on Android", "Get it today"],
        style_notes="dark background",
        reference_asset_ids=[ASSET_ID],
    )
    body = json.loads(answer)
    assert body == {"run_id": str(STUDIO_RUN_ID), "status": "queued", "platform": "x_post"}


def test_start_creative_without_copy_asks_for_an_image_without_text():
    requests = FakeRequests()

    start_creative(a_context(FakeReads(), requests), goal="A hero image", platform="og_image")

    [request] = requests.creatives
    assert (request.exact_copy, request.style_notes, request.reference_asset_ids) == ([], "", [])


@pytest.mark.parametrize(
    "arguments",
    [
        {"goal": "A post", "platform": "tiktok_video"},
        {"goal": "", "platform": "x_post"},
        {"goal": "A post", "platform": "x_post", "exact_copy": ["line one\nline two"]},
        {"goal": "A post", "platform": "x_post", "exact_copy": ["x" * 121]},
        {"goal": "A post", "platform": "x_post", "exact_copy": ["a"] * 7},
        {"goal": "A post", "platform": "x_post", "reference_asset_ids": ["not-a-uuid"]},
    ],
    ids=["unknown-platform", "no-goal", "line-break", "long-line", "many-lines", "bad-reference"],
)
def test_start_creative_refuses_what_does_not_fit_without_reaching_the_caller(arguments):
    requests = FakeRequests()

    answer = start_creative(a_context(FakeReads(), requests), **arguments)

    assert answer.startswith("error: the image request does not fit")
    assert requests.creatives == []
    # The refusal names the fields at fault and never a value the model wrote.
    assert "tiktok_video" not in answer
    assert "x" * 10 not in answer
    assert "not-a-uuid" not in answer


def test_an_unknown_platform_is_a_tool_error_the_turn_reads_and_corrects(reads):
    requests = FakeRequests()
    model = ScriptedModel(
        [call("start_creative", goal="A post", platform="tiktok")],
        [call("start_creative", goal="A post", platform="x_post")],
        [call(SUBMIT, answer=f"Started {STUDIO_RUN_ID}.")],
    )

    turn = answer_message(reads, model, history(), requests=requests)

    first = [m for m in model.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert first == "error: the image request does not fit: 'platform'"
    assert [request.platform for request in requests.creatives] == [Platform.X_POST]
    assert turn.answer == f"Started {STUDIO_RUN_ID}."


def test_start_creative_without_a_caller_says_starting_runs_is_not_available():
    answer = start_creative(a_context(FakeReads(), None), goal="A post", platform="x_post")

    assert answer == "error: starting a studio run is not available here"


def test_a_studio_run_that_could_not_be_started_is_an_error_the_turn_reads():
    refused = StudioNotStarted("a reference image is not in this project's Studio library")
    requests = FakeRequests(fails=refused)
    model = ScriptedModel(
        [call("start_creative", goal="A post", platform="x_post")],
        [call(SUBMIT, answer="I could not start it.")],
    )

    turn = answer_message(FakeReads(), model, history(), requests=requests)

    assert turn.answer == "I could not start it."
    assert model.tool_results()[0] == f"error: {refused}"


def test_a_turn_that_starts_a_studio_run_records_it_with_its_id(reads):
    events = RecordedEvents()
    model = ScriptedModel(
        [call("start_creative", goal="A post", platform="x_post", exact_copy=["Now on Android"])],
        [call(SUBMIT, answer=f"Started {STUDIO_RUN_ID}.")],
    )

    answer_message(reads, model, history(), requests=FakeRequests(), events=events)

    assert [event.kind for event in events.events] == [TOOL_CALLED, RUN_STARTED, TOOL_ANSWERED]
    assert events.events[0].detail["label"] == TOOL_LABELS["start_creative"]
    assert events.events[1].detail == {"run_kind": STUDIO_RUN_KIND, "run_id": str(STUDIO_RUN_ID)}


def test_every_platform_is_named_to_the_model():
    by_name = {schema["function"]["name"]: schema for schema in agent.tool_schemas()}
    description = by_name["start_creative"]["function"]["description"]
    for platform in Platform:
        assert platform.value in description
        assert platform.value in SYSTEM_PROMPT


def test_the_prompt_says_the_copy_is_the_operators_word_for_word():
    assert "verbatim" in SYSTEM_PROMPT
    assert "Never invent copy" in SYSTEM_PROMPT
    assert "Approvals" in SYSTEM_PROMPT
    assert "Studio library" in SYSTEM_PROMPT


def test_list_studio_runs_bounds_the_limit_and_carries_the_request(reads):
    context = a_context(reads, None)

    [run] = json.loads(list_studio_runs(context, limit=500))

    assert reads.studio_limits == [MAX_LISTED_RECORDS]
    assert (run["goal"], run["exact_copy"], run["platform"]) == (
        "Announce the Android launch",
        ["Now on Android"],
        "x_post",
    )
    assert run["final_asset_id"] == str(ASSET_ID)


def test_get_studio_run_reads_its_review_and_why_it_needs_one(reads):
    run = json.loads(get_studio_run(a_context(reads, None), str(STUDIO_RUN_ID)))

    assert run["status"] == "needs_review"
    assert run["review_reason"] == "the round limit"
    assert run["review"]["check_failures"] == ["copy"]
    assert run["review"]["failed_criteria"][0]["criterion"] == "legibility"


def test_get_studio_run_refuses_a_non_uuid_and_names_a_missing_run(reads):
    context = a_context(reads, None)

    assert get_studio_run(context, "latest").startswith("error: run_id must be a UUID")
    missing = uuid.uuid4()
    assert (
        get_studio_run(context, str(missing)) == f"error: no studio run {missing} in this project"
    )


def test_list_media_assets_bounds_the_limit(reads):
    [asset] = json.loads(list_media_assets(a_context(reads, None), limit=0))

    assert reads.asset_limits == [1]
    assert (asset["id"], asset["status"]) == (str(ASSET_ID), "draft")


def test_an_empty_studio_is_a_plain_sentence():
    context = a_context(FakeReads(empty=True), None)

    assert list_studio_runs(context) == "The project has no studio runs."
    assert list_media_assets(context) == "The project's Studio library is empty."


@pytest.mark.parametrize("status", ["succeeded", "needs_review", "failed"])
def test_a_studio_follow_up_is_told_to_read_the_run_and_link_in_words_only(status):
    instruction = follow_up_instruction(STUDIO_RUN_KIND, str(STUDIO_RUN_ID), status)

    assert f"get_studio_run and run_id {STUDIO_RUN_ID}" in instruction
    assert f"status {status}" in instruction
    assert "write no link" in instruction
    assert "cannot start another run" in instruction


def test_each_studio_outcome_is_explained_its_own_way():
    def told(status: str) -> str:
        return follow_up_instruction(STUDIO_RUN_KIND, str(STUDIO_RUN_ID), status)

    assert "accept or reject" in told("succeeded")
    assert "review_reason" in told("needs_review")
    assert "error" in told("failed")


def test_a_research_follow_up_is_still_told_to_read_the_report():
    instruction = follow_up_instruction(RESEARCH_RUN_KIND, str(RESEARCH_RUN_ID), "succeeded")

    assert "read_research_report" in instruction


def test_a_follow_up_on_another_kind_of_run_is_refused():
    with pytest.raises(ValueError):
        follow_up_instruction("analysis-run", str(RUN_ID), "succeeded")


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


# ------------------------------------------------------------ the turn's events
class RecordedEvents:
    """``TurnEvents`` that keep what the turn recorded, in order."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def record(self, event: RunEvent) -> None:
        self.events.append(event)


def test_a_turn_records_each_tool_before_it_runs_and_after_it_answers(reads):
    events = RecordedEvents()
    model = ScriptedModel([call("get_project_status")], [call(SUBMIT, answer="Fine.")])

    answer_message(reads, model, history(), events=events)

    assert events.events == [
        RunEvent(
            TOOL_CALLED, {"tool": "get_project_status", "label": TOOL_LABELS["get_project_status"]}
        ),
        RunEvent(TOOL_ANSWERED, {"tool": "get_project_status", "ok": True}),
    ]


def test_a_tool_that_answered_with_an_error_is_recorded_as_not_done(reads):
    events = RecordedEvents()
    model = ScriptedModel(
        [call("explain_proposal", proposal_id="not-a-uuid")], [call(SUBMIT, answer="No.")]
    )

    answer_message(reads, model, history(), events=events)

    assert events.events[-1] == RunEvent(TOOL_ANSWERED, {"tool": "explain_proposal", "ok": False})


def test_a_research_run_the_turn_started_is_recorded_with_its_id(reads):
    events = RecordedEvents()
    model = ScriptedModel(
        [call("start_research", questions=["Is it live?"])], [call(SUBMIT, answer="Started.")]
    )

    answer_message(reads, model, history(), requests=FakeRequests(), events=events)

    assert [event.kind for event in events.events] == [TOOL_CALLED, RUN_STARTED, TOOL_ANSWERED]
    assert events.events[1].detail == {
        "run_kind": RESEARCH_RUN_KIND,
        "run_id": str(RESEARCH_RUN_ID),
    }


def test_the_submission_records_no_event(reads):
    events = RecordedEvents()

    answer_message(reads, ScriptedModel([call(SUBMIT, answer="Fine.")]), history(), events=events)

    assert events.events == []


def test_a_turn_whose_caller_cannot_start_runs_is_not_offered_start_research(reads):
    model = ScriptedModel([call(SUBMIT, answer="Fine.")])

    answer_message(reads, model, history())

    offered = [schema["function"]["name"] for schema in model.requests[0]["tools"]]
    assert "start_research" not in offered
    assert SUBMIT in offered


def test_a_tool_the_agent_does_not_have_is_recorded_without_the_name_the_model_wrote(reads):
    events = RecordedEvents()
    model = ScriptedModel([call("drop_all_tables")], [call(SUBMIT, answer="No.")])

    answer_message(reads, model, history(), events=events)

    assert [event.detail["tool"] for event in events.events] == [UNKNOWN_TOOL, UNKNOWN_TOOL]
