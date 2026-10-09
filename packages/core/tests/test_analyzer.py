"""Tests for the analyzer agent, driven by a scripted model (no network).

The tests of resumable runs keep their checkpoints in PostgreSQL; the fixtures
are in the ``conftest.py`` at the repository root.
"""

import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import forget_checkpoints, run_checkpoints
from openmarketer_core.llm import ChatReply, LLMError
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.analyzer_agent import (
    Analysis,
    AnalysisError,
    Limits,
    ResumableRun,
    Resumption,
    analyze,
)
from openmarketer_core.repository_analysis.analyzer_agent.rules import (
    SUBMIT,
    StoredProgress,
    repository_map,
    resumption,
    submit_schema,
)
from openmarketer_core.repository_analysis.analyzer_agent.tools import RepoTools
from openmarketer_core.repository_analysis.extraction import ExtractedFact
from openmarketer_core.repository_analysis.intake import RepoFiles


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
        turn = self.turns.pop(0) if self.turns else [call("list_files")]
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, str):
            message = {"role": "assistant", "content": turn}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=self.cost)

    def tool_results(self) -> list[str]:
        return [m["content"] for m in self.requests[-1]["messages"] if m["role"] == "tool"]


@pytest.fixture
def files(tmp_path) -> RepoFiles:
    tree = {
        "README.md": "# Memoria\n\nShare memories offline.\n",
        "app/Share.kt": "\n".join(f"line {i}" for i in range(1, 41)) + "\n",
        "app/Theme.kt": 'val primary = "#1B6EF3"\n',
        ".env": "TOKEN=abc\n",
        "logo.png": "\x89PNG\x00\x00binary",
    }
    for path, content in tree.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return RepoFiles(tmp_path)


def profile(**overrides) -> dict:
    base = {
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
    return {**base, **overrides}


# -------------------------------------------------------------------- loop
def test_explores_then_submits(files):
    model = ScriptedModel(
        [call("read_file", path="README.md")],
        [call("search", pattern="primary", glob="*.kt"), call("list_files", glob="app/*")],
        [call(SUBMIT, **profile())],
    )
    result = analyze(files, model)
    assert result.profile.product.name == "Memoria"
    assert result.profile.features[0].status == "live"
    assert result.tool_calls == ["read_file", "search", "list_files", SUBMIT]
    assert (result.steps, result.model, result.notes) == (3, "fake/model", [])
    assert result.cost_usd == pytest.approx(0.03)
    assert all(r["role"] == "repo_analyzer" for r in model.requests)


def test_tool_results_reach_the_model(files):
    model = ScriptedModel(
        [call("read_file", path="README.md"), call("search", pattern="primary")],
        [call(SUBMIT, **profile())],
    )
    analyze(files, model)
    readme, search = model.tool_results()
    assert readme.startswith("1: # Memoria")
    assert search == 'app/Theme.kt:1: val primary = "#1B6EF3"'


def test_first_message_is_the_repository_map(files):
    fact = ExtractedFact(extractor="x", kind="manifest.name", value="Memoria", file="README.md")
    model = ScriptedModel([call(SUBMIT, **profile())])
    analyze(files, model, facts=[fact])
    first = model.requests[0]["messages"][1]["content"]
    assert '- manifest.name = "Memoria" (README.md)' in first
    assert ".env" not in first


def test_text_only_reply_is_nudged_back_to_the_tools(files):
    model = ScriptedModel("Let me think about this.", [call(SUBMIT, **profile())])
    result = analyze(files, model)
    assert result.steps == 2
    assert "call submit_profile" in model.requests[1]["messages"][-1]["content"]


def test_unparseable_arguments_are_reported_back(files):
    broken = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{"}}
    model = ScriptedModel([broken], [call(SUBMIT, **profile())])
    analyze(files, model)
    assert model.tool_results()[0].startswith("error: could not parse the arguments")


# ------------------------------------------------------------ verification
def test_invented_evidence_is_rejected_then_fixed(files):
    bad = profile()
    bad["features"][0]["evidence"] = [{"file": "app/Sharing.kt", "lines": "1-5"}]
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    result = analyze(files, model)
    assert result.steps == 2
    assert "app/Sharing.kt does not exist or cannot be read" in model.tool_results()[0]


def test_lines_beyond_the_end_of_the_file_are_rejected(files):
    bad = profile()
    bad["features"][0]["evidence"] = [{"file": "app/Share.kt", "lines": "30-400"}]
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    analyze(files, model)
    assert "app/Share.kt has 40 lines, but lines 30-400 were cited" in model.tool_results()[0]


def test_excluded_file_is_not_valid_evidence(files):
    bad = profile()
    bad["product"]["evidence"] = [{"file": ".env", "lines": "1"}]
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    analyze(files, model)
    assert "product: .env does not exist or cannot be read" in model.tool_results()[0]


def test_live_feature_needs_evidence(files):
    bad = profile()
    bad["features"][0]["evidence"] = []
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    analyze(files, model)
    assert "status is live but no evidence is cited" in model.tool_results()[0]


def test_missing_confidence_is_rejected_instead_of_defaulting_to_zero(files):
    bad = profile()
    del bad["features"][0]["confidence"]
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    result = analyze(files, model)
    assert "feature 'offline-sharing': confidence is missing" in model.tool_results()[0]
    assert result.profile.features[0].confidence == 0.8


def test_missing_confidence_on_the_last_attempt_is_noted(files):
    bad = profile()
    del bad["features"][0]["confidence"]
    result = analyze(files, ScriptedModel(*[[call(SUBMIT, **bad)]] * 3))
    assert result.notes == ["feature 'offline-sharing': no confidence was given, it is 0"]


def test_zero_confidence_next_to_evidence_is_rejected(files):
    bad = profile()
    bad["features"][0]["confidence"] = 0.0
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    analyze(files, model)
    assert "feature 'offline-sharing': confidence is missing or 0" in model.tool_results()[0]


def test_submit_schema_requires_confidence_and_shows_no_default():
    definitions = submit_schema()["function"]["parameters"]["$defs"]
    for name in ("Product", "Feature", "Brand", "Audience", "BusinessModel", "Measurement"):
        assert "confidence" in definitions[name]["required"]
        assert "default" not in definitions[name]["properties"]["confidence"]
    assert "confidence" not in ProductProfile.model_json_schema()["$defs"]["Feature"]["required"]


def test_schema_errors_are_explained_to_the_model(files):
    bad = profile()
    bad["features"][0]["confidence"] = 7
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    analyze(files, model)
    assert "features.0.confidence" in model.tool_results()[0]


def test_sections_sent_as_json_text_are_decoded(files):
    stringified = {
        key: json.dumps(value) if isinstance(value, dict | list) else value
        for key, value in profile().items()
    }
    result = analyze(files, ScriptedModel([call(SUBMIT, **stringified)]))
    assert result.profile == ProductProfile.model_validate(profile())


def test_last_attempt_is_repaired_in_code(files):
    bad = profile()
    bad["features"][0]["evidence"] = [{"file": "nope.kt"}]
    bad["product"]["evidence"].append({"file": "also/nope.md", "lines": "1"})
    model = ScriptedModel(*[[call(SUBMIT, **bad)]] * 3)
    result = analyze(files, model, limits=Limits(max_submissions=3))
    feature = result.profile.features[0]
    assert (feature.status, feature.evidence, feature.confidence) == ("unknown", [], 0.3)
    assert [e.file for e in result.profile.product.evidence] == ["README.md"]
    assert len(result.notes) == 3


def test_schema_invalid_on_the_last_attempt_fails(files):
    model = ScriptedModel(*[[call(SUBMIT, product={"name": ""})]] * 3)
    with pytest.raises(AnalysisError, match="does not match the schema"):
        analyze(files, model)


# ------------------------------------------------------------------ limits
def test_agent_is_forced_to_submit_when_steps_run_out(files):
    model = ScriptedModel()  # only ever lists files
    with pytest.raises(AnalysisError, match="no profile after 6 steps"):
        analyze(files, model, limits=Limits(max_steps=6))
    choices = [r["tool_choice"] for r in model.requests]
    assert choices[:4] == [None] * 4
    assert all(c == {"type": "function", "function": {"name": SUBMIT}} for c in choices[4:])
    wrap_up = [m for m in model.requests[-1]["messages"] if "Submit the profile now" in str(m)]
    assert len(wrap_up) == 1


def test_cost_limit_stops_the_run(files):
    model = ScriptedModel(cost=0.2)
    with pytest.raises(AnalysisError, match="cost limit reached"):
        analyze(files, model, limits=Limits(max_cost_usd=0.5))
    assert len(model.requests) == 3
    assert model.requests[-1]["tool_choice"] is not None  # was pushed to submit near the limit


def test_submission_made_out_of_budget_is_repaired_instead_of_rejected(files):
    bad = profile()
    bad["features"][0]["evidence"] = [{"file": "nope.kt"}]
    model = ScriptedModel([call("list_files")], [call(SUBMIT, **bad)], cost=0.4)
    result = analyze(files, model, limits=Limits(max_cost_usd=0.5))
    assert result.steps == 2
    assert result.profile.features[0].status == "unknown"
    assert result.notes


# ------------------------------------------------------------------- tools
def test_tools_cannot_reach_excluded_or_outside_files(files):
    tools = RepoTools(files)
    assert tools.read_file(".env") == "error: .env: excluded (env_file)"
    assert "outside_repository" in tools.read_file("../../etc/hosts")
    assert tools.read_file("missing.txt") == "error: no such file: missing.txt"
    assert tools.read_file("logo.png") == "error: logo.png is a binary file"
    assert ".env" not in tools.list_files()
    assert tools.search("TOKEN") == "no matches"


def test_read_file_pages_through_long_files(files, tmp_path):
    (tmp_path / "long.txt").write_text("\n".join(f"row {i}" for i in range(1, 601)))
    tools = RepoTools(RepoFiles(tmp_path))
    first = tools.read_file("long.txt")
    assert first.startswith("1: row 1\n") and "250: row 250" in first
    assert first.endswith("350 more lines; continue with start_line=251")
    assert tools.read_file("long.txt", start_line=599) == "599: row 599\n600: row 600"
    assert tools.read_file("long.txt", start_line=900) == "error: long.txt has 600 lines"


def test_bad_tool_input_never_raises(files):
    tools = RepoTools(files)
    assert tools.call("delete_everything", {}) == "error: unknown tool 'delete_everything'"
    assert tools.call("read_file", {"nope": 1}).startswith("error: bad arguments for read_file")
    assert tools.search("(unclosed").startswith("error: invalid regular expression")
    assert tools.list_files("*.swift") == "no files match"


def test_repository_map_is_bounded(files):
    facts = [
        ExtractedFact(extractor="x", kind="repo.note", value="v" * 400, file="README.md")
        for _ in range(100)
    ]
    text = repository_map(files, facts)
    assert len(text) < 8200 and text.endswith("use list_files for the rest")


# ------------------------------------------------------------------- graph
def test_analyzer_is_a_langgraph_graph(files):
    from langgraph.graph.state import CompiledStateGraph

    from openmarketer_core.repository_analysis.analyzer_agent.graph import build_graph

    graph = build_graph(ScriptedModel(), RepoTools(files), Limits())
    assert isinstance(graph, CompiledStateGraph)
    assert {"call_model", "run_tools"} <= set(graph.get_graph().nodes)


def test_rules_module_does_not_depend_on_the_framework():
    import inspect

    import openmarketer_core.repository_analysis.analyzer_agent.rules as rules
    import openmarketer_core.repository_analysis.analyzer_agent.tools as tools_module

    for module in (rules, tools_module):
        assert "import langgraph" not in inspect.getsource(module)
        assert "from langgraph" not in inspect.getsource(module)


# ------------------------------------------------------------- resumption
def test_attempt_with_nothing_stored_starts_fresh():
    assert resumption(None, "commit-a") is Resumption.FRESH


def test_attempt_continues_unfinished_progress_on_the_same_commit():
    stored = StoredProgress(commit_sha="commit-a", finished=False)
    assert resumption(stored, "commit-a") is Resumption.CONTINUED


def test_attempt_returns_a_finished_analysis_of_the_same_commit():
    stored = StoredProgress(commit_sha="commit-a", finished=True)
    assert resumption(stored, "commit-a") is Resumption.ALREADY_FINISHED


@pytest.mark.parametrize("finished", [False, True])
def test_attempt_starts_over_when_progress_is_of_another_commit(finished):
    stored = StoredProgress(commit_sha="commit-a", finished=finished)
    assert resumption(stored, "commit-b") is Resumption.STARTED_OVER


# ---------------------------------------------------------- resumable runs
COMMIT = "1f0c3a9"
OUTAGE = LLMError("provider is down", status=503)
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


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
    with run_checkpoints(database_url, f"run-{uuid.uuid4()}") as checkpoints:
        yield checkpoints


def attempt(files, model, run: RunCheckpoints, commit: str = COMMIT, **limits) -> Analysis:
    resume = ResumableRun(run, commit_sha=commit)
    return analyze(files, model, limits=Limits(**limits), resume=resume)


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


def test_run_with_checkpoints_gives_the_same_result_as_one_without(files, run):
    turns = exploration()
    plain = analyze(files, ScriptedModel(*turns))
    checkpointed = attempt(files, ScriptedModel(*turns), run)
    assert checkpointed == plain
    assert checkpointed.resumption is Resumption.FRESH


def test_run_with_checkpoints_stores_a_checkpoint_for_each_step(files, run, engine):
    attempt(files, ScriptedModel(*exploration()), run)
    # The input and the start, then call_model and run_tools for each of the three turns.
    assert stored_rows(engine, run)["checkpoints"] == 8


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

    assert result.resumption is Resumption.CONTINUED
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
        resumed = attempt(files, ScriptedModel(third), run)
    assert resumed.resumption is Resumption.CONTINUED
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
    assert again.resumption is Resumption.CONTINUED
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


@pytest.mark.parametrize("lost", range(1, 9))
def test_step_whose_checkpoint_was_not_stored_is_not_applied_twice(files, run, monkeypatch, lost):
    # A step's result is recorded first and the checkpoint that contains it second. Here the
    # attempt dies between the two, at each of the eight checkpoints of the analysis in turn.
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


def test_progress_on_another_commit_is_discarded_and_the_analysis_starts_over(
    files, run, engine, caplog
):
    turns = exploration()
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(turns[0], turns[1], OUTAGE), run, commit="old-commit")

    model = ScriptedModel(*turns)
    with caplog.at_level(
        logging.INFO, logger="openmarketer_core.repository_analysis.analyzer_agent.graph"
    ):
        result = attempt(files, model, run, commit="new-commit")

    assert result.resumption is Resumption.STARTED_OVER
    assert (result.steps, len(model.requests)) == (3, 3)
    assert [m["role"] for m in model.requests[0]["messages"]] == ["system", "user"]
    assert "are not of commit new-commit: starting over" in caplog.text


def test_starting_over_leaves_no_rows_of_the_discarded_progress(files, run, engine):
    turns = exploration()
    attempt(files, ScriptedModel(*turns), run, commit="new-commit")
    of_one_analysis = stored_rows(engine, run)
    run.forget()

    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(turns[0], turns[1], OUTAGE), run, commit="old-commit")
    attempt(files, ScriptedModel(*turns), run, commit="new-commit")

    assert stored_rows(engine, run) == of_one_analysis


def test_finished_run_returns_its_result_without_asking_the_model(files, run):
    first = attempt(files, ScriptedModel(*exploration()), run)
    model = ScriptedModel()
    again = attempt(files, model, run)
    assert model.requests == []
    assert again.resumption is Resumption.ALREADY_FINISHED
    assert (again.profile, again.steps, again.model, again.tool_calls, again.notes) == (
        first.profile,
        first.steps,
        first.model,
        first.tool_calls,
        first.notes,
    )
    assert again.cost_usd == pytest.approx(first.cost_usd)


def test_finished_run_of_another_commit_is_analysed_again(files, run):
    attempt(files, ScriptedModel(*exploration()), run, commit="old-commit")
    model = ScriptedModel(*exploration())
    result = attempt(files, model, run, commit="new-commit")
    assert result.resumption is Resumption.STARTED_OVER
    assert len(model.requests) == 3


def test_forgotten_run_starts_from_the_beginning(files, run):
    turns = exploration()
    with pytest.raises(LLMError):
        attempt(files, ScriptedModel(turns[0], OUTAGE), run)
    run.forget()
    model = ScriptedModel(*turns)
    result = attempt(files, model, run)
    assert result.resumption is Resumption.FRESH
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
    result = attempt(files, model, other)

    assert result.resumption is Resumption.FRESH
    assert len(model.requests) == 3
    assert attempt(files, ScriptedModel(turns[2]), run).resumption is Resumption.CONTINUED


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
