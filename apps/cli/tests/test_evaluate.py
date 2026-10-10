"""Tests for ``openmarketer evaluate``: what it does before it may spend, and re-scoring.

No model is called and nothing is cloned: the benchmark itself is replaced
where a test reaches it (``packages/evaluation/tests`` runs the real one).
"""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openmarketer_cli import main
from openmarketer_core.llm import RouterChatModel
from openmarketer_evaluation import benchmark
from openmarketer_evaluation.benchmark import rescore
from openmarketer_evaluation.cases import load_cases
from openmarketer_evaluation.claude_code_judge import ClaudeCodeJudge
from openmarketer_evaluation.results import ResultStore

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "evaluation" / "tests" / "fixtures"
CASES = ["--cases-dir", str(FIXTURES / "cases")]


@pytest.fixture(autouse=True)
def configuration(monkeypatch, tmp_path) -> None:
    """The model configuration of the repository, with no override from this machine.

    The command runs in a temporary folder, so that a test which leaves the
    results folder at its default writes nothing into the checkout.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_CONFIG_PATH", str(ROOT / "config" / "models.yaml"))
    for name in ("LLM_MODEL__JUDGE", "LLM_MODEL__REPO_ANALYZER", "LLM_MODEL_FAST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_MODEL_STRONG", "vendor/judge-model")
    monkeypatch.setenv("LLM_MODEL__REPO_ANALYZER", "vendor/analyzer-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key-of-the-test")


@pytest.fixture
def benchmarks(monkeypatch) -> list[dict]:
    """Replace the benchmark; return the arguments of every time it was started."""
    started: list[dict] = []

    def run_benchmark(cases, **arguments):
        started.append({"cases": cases, **arguments})
        arguments["store"].write_config(arguments["config"])
        return rescore(cases, ResultStore(FIXTURES / "results" / "example-run")).scores

    monkeypatch.setattr(main, "run_benchmark", run_benchmark)
    monkeypatch.setattr(main, "discover_extractors", lambda: [])
    return started


@pytest.fixture
def spending(monkeypatch) -> list[str]:
    """The real benchmark, with its two spending steps replaced; return what was spent on."""
    spent: list[str] = []
    monkeypatch.setattr(benchmark, "run_analyzer", lambda *_, **__: spent.append("analyzer"))
    monkeypatch.setattr(benchmark, "judge_run", lambda *_, **__: spent.append("judge"))
    monkeypatch.setattr(main, "discover_extractors", lambda: [])
    return spent


def run(*args: str):
    return CliRunner().invoke(main.app, ["evaluate", *CASES, *args])


def test_without_the_flag_it_says_what_it_would_do_and_spends_nothing(spending, tmp_path):
    result = run("--results-dir", str(tmp_path / "results"))
    assert result.exit_code == 0
    assert "plan: 3 analyzer runs (3 per case)" in result.stderr
    assert "at most 99 judge calls" in result.stderr
    assert "nothing was run. Pass --live" in result.stderr
    assert spending == []
    assert not (tmp_path / "results").exists()


def test_plan_names_the_models_of_both_roles_and_the_provenance_of_each_label(benchmarks):
    result = run("--runs", "1")
    assert "analyzer: role repo_analyzer -> vendor/analyzer-model (env)" in result.stderr
    assert "judge:    role judge -> vendor/judge-model (env-tier:strong)" in result.stderr
    assert "example: commit 1111111111, label written_by_person, 1 analyzer runs" in result.stderr


def test_live_run_is_started_only_with_the_flag(benchmarks, tmp_path):
    result = run("--live", "--runs", "2", "--results-dir", str(tmp_path / "results"))
    assert result.exit_code == 0, result.stderr
    (started,) = benchmarks
    config = started["config"]
    assert started["live"] is True
    assert (config.runs_per_case, config.analyzer.model, config.judge.model) == (
        2,
        "vendor/analyzer-model",
        "vendor/judge-model",
    )
    assert [case.name for case in config.cases] == ["example"]
    (folder,) = (tmp_path / "results").iterdir()
    assert folder.name == config.benchmark_id
    assert {path.name for path in folder.iterdir()} == {
        ".gitignore",
        "config.json",
        "scores.json",
        "report.md",
    }
    assert "# Analyzer evaluation" in result.stdout


@pytest.mark.parametrize("flags", [["--live"], []])
def test_judge_that_is_the_analyzers_model_is_refused_before_anything_runs(
    spending, monkeypatch, tmp_path, flags
):
    monkeypatch.setenv("LLM_MODEL__JUDGE", "vendor/analyzer-model")
    result = run(*flags, "--results-dir", str(tmp_path / "results"))
    assert result.exit_code == 1
    assert "the judge and the analyzer both resolve to vendor/analyzer-model" in result.stderr
    assert spending == []
    assert not (tmp_path / "results").exists()


def test_shared_fallback_of_both_roles_is_warned_about(benchmarks):
    result = run()
    assert "warning: anthropic/claude-haiku-5.5 can answer for both roles" in result.stderr


def test_live_run_without_a_provider_key_stops_before_anything_runs(benchmarks, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    result = run("--live")
    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY is not set" in result.stderr
    assert benchmarks == []


def test_unknown_case_stops_the_command(benchmarks):
    result = run("--case", "nothing-like-it")
    assert result.exit_code == 1
    assert "unknown case nothing-like-it; there is: example" in result.stderr


def test_stored_run_is_scored_again_without_a_model_or_the_flag(benchmarks, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setattr(RouterChatModel, "from_env", lambda: pytest.fail("a model was built"))
    stored = tmp_path / "example-run"
    shutil.copytree(FIXTURES / "results" / "example-run", stored)
    result = run("--rescore", str(stored))
    assert result.exit_code == 0, result.stderr
    assert "scored again without a model call" in result.stderr
    assert "| features_live_but_expected_not_live | 1 of 2 | 1 | 1 | 1 | 1, - |" in result.stdout
    assert (stored / "scores.json").exists() and (stored / "report.md").exists()
    assert benchmarks == []


def test_scoring_again_a_folder_that_is_not_a_stored_run_stops_the_command(tmp_path):
    result = run("--rescore", str(tmp_path))
    assert result.exit_code == 1
    assert "config.json is missing" in result.stderr


def test_shipped_case_is_what_the_command_runs_by_default():
    assert [case.name for case in load_cases(ROOT / "evals" / "cases")] == ["excalidraw"]


def test_scoring_again_says_when_and_by_which_code(tmp_path):
    stored = tmp_path / "example-run"
    shutil.copytree(FIXTURES / "results" / "example-run", stored)
    result = run("--rescore", str(stored))
    assert "**scored again on " in result.stdout
    assert "not those the run itself computed" in result.stdout
    assert '"rescored": {' in (stored / "scores.json").read_text()


# ------------------------------------------------------ the judge's route
def test_judge_through_claude_code_is_named_in_the_plan_with_what_it_spends(spending):
    result = run("--judge", "claude-code")
    assert result.exit_code == 0
    assert "judge:    Claude Code (claude -p), model left to Claude Code" in result.stderr
    assert "spends Claude Code subscription usage, not model provider credit" in result.stderr
    assert "temperature, max_tokens, structured_output, fallbacks are not applied" in result.stderr
    assert "nothing was run. Pass --live" in result.stderr
    assert spending == []


def test_judge_through_claude_code_still_needs_the_flag(benchmarks, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert run("--judge", "claude-code").exit_code == 0
    (started,) = benchmarks
    assert started["live"] is False


def test_live_run_through_claude_code_records_the_route_and_gets_that_judge(
    benchmarks, monkeypatch, tmp_path
):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    result = run(
        "--live", "--judge", "claude-code", "--judge-model", "some-model",
        "--judge-timeout", "60", "--results-dir", str(tmp_path / "results"),
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    (started,) = benchmarks
    judge = started["judge"]
    assert isinstance(judge, ClaudeCodeJudge)
    assert (judge.model, judge.timeout_s) == ("some-model", 60.0)
    assert isinstance(started["analyzer"].model, RouterChatModel)
    route = started["config"].judge_route
    assert (route.route, route.cost_known, route.requested_model) == (
        "claude_code",
        False,
        "some-model",
    )
    assert route.role_settings_not_applied == [
        "temperature",
        "max_tokens",
        "structured_output",
        "fallbacks",
    ]
    assert "via **claude_code**, model some-model" in result.stdout


def test_default_judge_is_the_judge_role_through_the_model_router(benchmarks):
    run("--live")
    (started,) = benchmarks
    assert isinstance(started["judge"], RouterChatModel)
    assert started["config"].judge_route.route == "model_router"


def test_live_run_through_claude_code_stops_when_it_is_not_installed(benchmarks, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = run("--live", "--judge", "claude-code")
    assert result.exit_code == 1
    assert "needs Claude Code" in result.stderr
    assert benchmarks == []


def test_claude_code_judge_asked_for_the_analyzers_model_is_refused(spending, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    result = run("--live", "--judge", "claude-code", "--judge-model", "analyzer-model")
    assert result.exit_code == 1
    assert "the judge and the analyzer both resolve to" in result.stderr
    assert spending == []


def test_judge_model_without_the_claude_code_route_is_refused(benchmarks):
    result = run("--judge-model", "some-model")
    assert result.exit_code == 1
    assert "--judge-model goes with --judge claude-code" in result.stderr


def test_unknown_judge_route_is_refused(benchmarks):
    assert run("--judge", "somewhere-else").exit_code == 2


def test_last_line_says_what_the_chosen_route_would_spend(spending):
    assert "the judge's model provider credit." in run().stderr
    assert "the judge's Claude Code subscription usage." in run("--judge", "claude-code").stderr


def test_judge_model_that_starts_with_a_dash_is_refused(benchmarks):
    result = run("--judge", "claude-code", "--judge-model=--dangerously-skip-permissions")
    assert result.exit_code == 1
    assert "a name does not start with a dash" in result.stderr
    assert benchmarks == []
