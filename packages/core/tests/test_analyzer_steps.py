"""Tests for the analyzer's graph taken step by step, as a workflow takes it: with checkpoints.

The steps are ``AgentSteps`` of the analyzer's agent on the real checkpoint store in PostgreSQL
(the fixtures are in the ``conftest.py`` at the repository root); the model is scripted and the
clone the graph asks for is answered at once, with a commit. An attempt here is what the worker
makes of one: the steps of the run until its graph finishes or fails. The workflow, the clone
itself and storing the profile are the worker's, and are tested there.
"""

import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import forget_checkpoints, run_checkpoints
from openmarketer_core.llm import ChatReply, LLMError
from openmarketer_core.repository_analysis.analyzer_agent import Analysis, AnalysisError, Limits
from openmarketer_core.repository_analysis.analyzer_agent.definitions import (
    CLONE_REPOSITORY,
    AnalyzerContext,
    agent,
    analysis_of,
    initial_state,
)
from openmarketer_core.repository_analysis.analyzer_agent.graph import analyze, recursion_limit
from openmarketer_core.repository_analysis.analyzer_agent.rules import SUBMIT
from openmarketer_core.repository_analysis.intake import RepoFiles
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYSIS_RUN_KIND,
    AnalyzeRepositoryInput,
)

COMMIT = "1f0c3a9" + "0" * 33
OUTAGE = LLMError("provider is down", status=503)
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
# The run every thread of these tests is told it is: the graph only passes it on to its clone.
RUN = AnalyzeRepositoryInput(
    workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4()
)


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
        self.requests.append({"role": role, "messages": [dict(m) for m in messages]})
        turn = self.turns.pop(0) if self.turns else [call("list_files")]
        if isinstance(turn, Exception):
            raise turn
        message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=self.cost)


@pytest.fixture
def files(tmp_path) -> RepoFiles:
    tree = {
        "README.md": "# Memoria\n\nShare memories offline.\n",
        "app/Share.kt": "\n".join(f"line {i}" for i in range(1, 41)) + "\n",
        "app/Theme.kt": 'val primary = "#1B6EF3"\n',
    }
    for path, content in tree.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return RepoFiles(tmp_path)


def profile() -> dict:
    return {
        "product": {
            "name": "Memoria",
            "type": "consumer_app",
            "platforms": ["android", "ios"],
            "evidence": [{"file": "README.md", "lines": "1-3"}],
            "confidence": 0.9,
        },
        "features": [
            {
                "id": "offline-sharing",
                "description": "Share memories without a server",
                "status": "live",
                "evidence": [{"file": "app/Share.kt", "lines": "10-30"}],
                "confidence": 0.8,
            }
        ],
    }


def exploration() -> list:
    """Three model turns that end in an accepted profile."""
    return [
        [call("read_file", path="README.md")],
        [call("search", pattern="primary", glob="*.kt"), call("list_files", glob="app/*")],
        [call(SUBMIT, **profile())],
    ]


@pytest.fixture
def run(engine: Engine) -> Iterator[RunCheckpoints]:
    database_url = engine.url.render_as_string(hide_password=False)
    with run_checkpoints(database_url, f"{ANALYSIS_RUN_KIND}:{uuid.uuid4()}") as checkpoints:
        yield checkpoints


def steps_of(files: RepoFiles, model, limits: Limits, finished: list[Analysis]) -> AgentSteps:
    """The analyzer's steps as a worker wires them, with the repository already on its disk."""
    return AgentSteps(
        agent,
        initial_state=lambda run: initial_state(RUN),
        finish=lambda run, final: finished.append(analysis_of(final)) or {},
        context=lambda run, held: AnalyzerContext(
            model=model, limits=limits, files_at=lambda commit: files, facts_of=lambda read: []
        ),
        recursion_limit=recursion_limit(limits),
    )


def attempt(files, model, run: RunCheckpoints, **limits) -> Analysis:
    """One attempt at a run: its steps until the graph finishes, the clone answered at once."""
    finished: list[Analysis] = []
    steps = steps_of(files, model, Limits(**limits), finished)
    ref = GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=RUN.run_id)
    step = steps.advance(ref, run, None)
    if step.action is not None:
        cloned = ActionOutcome(
            step.action.action_id, succeeded=True, result={"commit_sha": COMMIT}, error=""
        )
        steps.advance(ref, run, cloned)
    (analysis,) = finished
    return analysis


def stored_rows(engine: Engine, run: RunCheckpoints) -> dict[str, int]:
    """How many rows each checkpoint table holds for the run's thread."""
    with engine.connect() as conn:
        return {
            table: conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE thread_id = :thread"),
                {"thread": run.thread_id},
            ).scalar_one()
            for table in CHECKPOINT_TABLES
        }


# ------------------------------------------------------------------ the clone
def test_first_step_asks_for_the_clone_of_the_run_and_names_nothing_else(files, run):
    steps = steps_of(files, ScriptedModel(), Limits(), [])
    step = steps.advance(GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=RUN.run_id), run, None)
    assert step.action is not None
    assert (step.action.name, step.action.payload) == (
        CLONE_REPOSITORY,
        {
            "workspace_id": str(RUN.workspace_id),
            "project_id": str(RUN.project_id),
            "run_id": str(RUN.run_id),
        },
    )


def test_model_is_not_asked_before_the_repository_is_cloned(files, run):
    model = ScriptedModel()
    steps = steps_of(files, model, Limits(), [])
    steps.advance(GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=RUN.run_id), run, None)
    assert model.requests == []


def test_clone_that_failed_ends_the_analysis_with_what_the_action_said(files, run):
    steps = steps_of(files, ScriptedModel(), Limits(), [])
    ref = GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=RUN.run_id)
    asked = steps.advance(ref, run, None).action
    assert asked is not None
    refused = ActionOutcome(
        asked.action_id, succeeded=False, result={}, error="the repository was not found"
    )
    with pytest.raises(AnalysisError) as raised:
        steps.advance(ref, run, refused)
    assert str(raised.value) == "the repository was not found"


def test_files_are_asked_for_at_the_commit_that_was_cloned(files, run):
    asked_for: list[str] = []

    def files_at(commit: str) -> RepoFiles:
        asked_for.append(commit)
        return files

    limits = Limits()
    steps = AgentSteps(
        agent,
        initial_state=lambda run: initial_state(RUN),
        finish=lambda run, final: {},
        context=lambda run, held: AnalyzerContext(
            model=ScriptedModel(*exploration()),
            limits=limits,
            files_at=files_at,
            facts_of=lambda read: [],
        ),
        recursion_limit=recursion_limit(limits),
    )
    ref = GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=RUN.run_id)
    asked = steps.advance(ref, run, None).action
    assert asked is not None
    steps.advance(ref, run, ActionOutcome(asked.action_id, True, {"commit_sha": COMMIT}, ""))
    # Once in the attempt, and the commit of the clone: never "whatever the branch is now".
    assert asked_for == [COMMIT]


# ----------------------------------------------------------------- attempts
def test_run_with_checkpoints_gives_the_same_result_as_one_without(files, run):
    turns = exploration()
    plain = analyze(files, ScriptedModel(*turns))
    checkpointed = attempt(files, ScriptedModel(*turns), run)
    assert checkpointed == plain


def test_run_with_checkpoints_stores_a_checkpoint_for_each_step(files, run, engine):
    attempt(files, ScriptedModel(*exploration()), run)
    # The input and the start, the clone and the preparation, then call_model and run_tools
    # for each of the three turns.
    assert stored_rows(engine, run)["checkpoints"] == 10


def test_model_failure_reaches_the_caller_unchanged(files, run):
    with pytest.raises(LLMError) as raised:
        attempt(files, ScriptedModel(exploration()[0], OUTAGE), run)
    assert raised.value is OUTAGE
    assert (str(raised.value), raised.value.status) == ("provider is down", 503)


def test_reaching_a_limit_is_an_analysis_error_with_checkpoints(files, run):
    with pytest.raises(AnalysisError) as raised:
        attempt(files, ScriptedModel(), run, max_steps=2)
    assert str(raised.value) == "no profile after 2 steps"


def test_next_attempt_continues_where_the_failed_one_stopped(files, run):
    turns = exploration()
    uninterrupted = ScriptedModel(*turns)
    expected = analyze(files, uninterrupted)

    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(turns[0], turns[1], OUTAGE), run)
    second = ScriptedModel(turns[2])
    result = attempt(files, second, run)

    assert len(second.requests) == 1
    assert second.requests[0]["messages"] == uninterrupted.requests[2]["messages"]
    assert (result.profile, result.steps, result.tool_calls) == (
        expected.profile,
        expected.steps,
        expected.tool_calls,
    )
    assert result.cost_usd == pytest.approx(expected.cost_usd)


def test_resuming_reads_nothing_the_strict_serializer_refuses(files, run, caplog):
    first, second, third = exploration()
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(first, second, LLMError("HTTP 503", status=503)), run)
    with caplog.at_level(logging.WARNING, logger="langgraph"):
        attempt(files, ScriptedModel(third), run)
    assert "Blocked deserialization" not in caplog.text


def test_attempt_after_checkpoints_were_removed_midway_continues_from_what_was_stored_since(
    files, run, engine
):
    first, second, third = exploration()

    class LosesItsCheckpointsMidway(ScriptedModel):
        def chat(self, role, messages, *, tools=None, tool_choice=None):
            if len(self.requests) == 1:
                forget_checkpoints(engine.url.render_as_string(hide_password=False), run.thread_id)
            return super().chat(role, messages, tools=tools, tool_choice=tool_choice)

    outage = LLMError("HTTP 503", status=503)
    with pytest.raises(LLMError):
        attempt(files, LosesItsCheckpointsMidway(first, second, outage), run)

    model = ScriptedModel(third)
    again = attempt(files, model, run)
    assert (again.steps, len(model.requests)) == (3, 1)


def test_step_limit_counts_the_steps_of_earlier_attempts(files, run):
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(*exploration()[:2], OUTAGE), run, max_steps=3)
    never_submits = ScriptedModel()
    with pytest.raises(AnalysisError, match="no profile after 3 steps"):
        attempt(files, never_submits, run, max_steps=3)
    assert len(never_submits.requests) == 1


def test_cost_limit_counts_the_cost_of_earlier_attempts(files, run):
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(*exploration()[:2], OUTAGE, cost=0.2), run, max_cost_usd=0.5)
    never_submits = ScriptedModel(cost=0.2)
    with pytest.raises(AnalysisError, match=r"cost limit reached \(\$0.600\)"):
        attempt(files, never_submits, run, max_cost_usd=0.5)
    assert len(never_submits.requests) == 1


def test_submission_limit_counts_the_submissions_of_earlier_attempts(files, run):
    bad = profile()
    del bad["features"][0]["confidence"]
    rejected = [call(SUBMIT, **bad)]
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(rejected, rejected, OUTAGE), run)
    result = attempt(files, ScriptedModel(rejected), run)
    assert result.notes == ["feature 'offline-sharing': no confidence was given, it is 0"]


def test_step_that_failed_is_not_stored_twice_when_it_is_retried(files, run):
    turns = exploration()
    uninterrupted = ScriptedModel(*turns)
    expected = analyze(files, uninterrupted)

    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(turns[0], OUTAGE), run)
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(OUTAGE), run)
    third = ScriptedModel(turns[1], turns[2])
    result = attempt(files, third, run)

    assert result.tool_calls == expected.tool_calls
    assert [r["messages"] for r in third.requests] == [
        r["messages"] for r in uninterrupted.requests[1:]
    ]


@pytest.mark.parametrize("lost", range(1, 11))
def test_step_whose_checkpoint_was_not_stored_is_not_applied_twice(files, run, monkeypatch, lost):
    # A step's result is recorded first and the checkpoint that contains it second. Here the
    # attempt dies between the two, at each of the ten checkpoints of the analysis in turn.
    turns = exploration()
    uninterrupted = ScriptedModel(*turns)
    expected = analyze(files, uninterrupted)
    store_checkpoint = run.store.put
    checkpoints = []

    def lose_one_checkpoint(*checkpoint):
        checkpoints.append(checkpoint)
        if len(checkpoints) == lost:
            raise DatabaseError("connection lost")
        return store_checkpoint(*checkpoint)

    monkeypatch.setattr(run.store, "put", lose_one_checkpoint)
    first = ScriptedModel(*turns)
    with pytest.raises(DatabaseError, match="connection lost"):
        attempt(files, first, run)
    monkeypatch.undo()
    second = ScriptedModel(*turns[len(first.requests) :])
    result = attempt(files, second, run)

    assert len(first.requests) + len(second.requests) == len(uninterrupted.requests)
    assert (result.profile, result.steps, result.tool_calls) == (
        expected.profile,
        expected.steps,
        expected.tool_calls,
    )
    assert result.cost_usd == pytest.approx(expected.cost_usd)
    if second.requests:
        assert second.requests[-1]["messages"] == uninterrupted.requests[-1]["messages"]


def test_finished_run_returns_its_result_without_asking_the_model(files, run):
    first = attempt(files, ScriptedModel(*exploration()), run)
    model = ScriptedModel()
    again = attempt(files, model, run)
    assert model.requests == []
    assert (again.profile, again.steps, again.model, again.tool_calls, again.notes) == (
        first.profile,
        first.steps,
        first.model,
        first.tool_calls,
        first.notes,
    )
    assert again.cost_usd == pytest.approx(first.cost_usd)


def test_forgotten_run_starts_from_the_beginning(files, run):
    turns = exploration()
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(turns[0], OUTAGE), run)
    run.forget()
    model = ScriptedModel(*turns)
    attempt(files, model, run)
    assert len(model.requests) == 3


def test_forgetting_a_run_removes_its_rows_and_leaves_another_runs(files, run, engine):
    other = RunCheckpoints(store=run.store, thread_id=f"run-{uuid.uuid4()}")
    attempt(files, ScriptedModel(*exploration()), run)
    attempt(files, ScriptedModel(*exploration()), other)
    of_the_other = stored_rows(engine, other)

    run.forget()

    assert stored_rows(engine, run) == dict.fromkeys(CHECKPOINT_TABLES, 0)
    assert stored_rows(engine, other) == of_the_other
    assert all(of_the_other.values())


def test_forgetting_a_run_with_no_checkpoints_does_nothing(files, run, engine):
    attempt(files, ScriptedModel(*exploration()), run)
    run.forget()
    run.forget()
    assert stored_rows(engine, run) == dict.fromkeys(CHECKPOINT_TABLES, 0)


def test_runs_do_not_see_each_others_progress(files, run):
    turns = exploration()
    other = RunCheckpoints(store=run.store, thread_id=f"run-{uuid.uuid4()}")
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(turns[0], turns[1], OUTAGE), run)

    model = ScriptedModel(*turns)
    attempt(files, model, other)

    assert len(model.requests) == 3
    continued = ScriptedModel(turns[2])
    attempt(files, continued, run)
    assert len(continued.requests) == 1


def test_checkpoint_that_cannot_be_stored_stops_the_run_before_the_next_step(
    files, run, monkeypatch
):
    def refuse(*checkpoint):
        raise DatabaseError("connection lost")

    monkeypatch.setattr(run.store, "put", refuse)
    model = ScriptedModel(*exploration())
    with pytest.raises(DatabaseError, match="connection lost"):
        attempt(files, model, run)
    assert model.requests == []
