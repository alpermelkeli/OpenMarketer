"""Tests for the analyzer agent, driven by a scripted model (no network)."""

import json

import pytest

from openmarketer_core.analyzer import AnalysisError, Limits, analyze
from openmarketer_core.analyzer.rules import SUBMIT, repository_map
from openmarketer_core.analyzer.tools import RepoTools
from openmarketer_core.extraction import ExtractedFact
from openmarketer_core.intake import RepoFiles
from openmarketer_core.llm import ChatReply


def call(name: str, **arguments) -> dict:
    return {"id": f"c{id(arguments)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip


class ScriptedModel:
    """Replays prepared assistant turns and records what it was sent."""

    def __init__(self, *turns, cost: float = 0.01):
        self.turns = list(turns)
        self.cost = cost
        self.requests: list[dict] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None):
        self.requests.append(
            {"role": role, "messages": [dict(m) for m in messages], "tool_choice": tool_choice}
        )
        turn = self.turns.pop(0) if self.turns else [call("list_files")]
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


def test_schema_errors_are_explained_to_the_model(files):
    bad = profile()
    bad["features"][0]["confidence"] = 7
    model = ScriptedModel([call(SUBMIT, **bad)], [call(SUBMIT, **profile())])
    analyze(files, model)
    assert "features.0.confidence" in model.tool_results()[0]


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

    from openmarketer_core.analyzer.graph import build_graph

    graph = build_graph(ScriptedModel(), RepoTools(files), Limits())
    assert isinstance(graph, CompiledStateGraph)
    assert {"call_model", "run_tools"} <= set(graph.get_graph().nodes)


def test_rules_module_does_not_depend_on_the_framework():
    import inspect

    import openmarketer_core.analyzer.rules as rules
    import openmarketer_core.analyzer.tools as tools_module

    for module in (rules, tools_module):
        assert "import langgraph" not in inspect.getsource(module)
        assert "from langgraph" not in inspect.getsource(module)
