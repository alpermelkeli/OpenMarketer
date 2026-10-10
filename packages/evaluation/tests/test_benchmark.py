"""Tests for the benchmark as one operation: a scripted model on a local repository, then files."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from openmarketer_core.llm import LLMError
from openmarketer_core.repository_analysis.analyzer_agent import Limits
from openmarketer_evaluation.benchmark import (
    NotLive,
    benchmark_config,
    case_record,
    code_version,
    plan,
    rescore,
    run_benchmark,
)
from openmarketer_evaluation.cases import load_cases
from openmarketer_evaluation.judge import SameModelError
from openmarketer_evaluation.report import render
from openmarketer_evaluation.results import (
    AnalyzerLimits,
    BenchmarkConfig,
    JudgeRoute,
    ResultsError,
    ResultStore,
    RoleModel,
    RunEnding,
)
from openmarketer_evaluation.runner import AnalyzerSetup
from openmarketer_evaluation.verdicts import VerdictState

FIXTURES = Path(__file__).parent / "fixtures"
MATCH = json.dumps({"matches": [{"expected": "E1", "drafted": "D1", "kind": "same"}]})
SUPPORTED = '{"support": "supported", "reason": "shown"}'


def role(name: str, model: str) -> RoleModel:
    return RoleModel(
        role=name, model=model, source="role", fallbacks=[], temperature=0.0, max_tokens=1024
    )


def config_for(case, runs: int) -> BenchmarkConfig:
    return BenchmarkConfig(
        benchmark_id="test-run",
        started_at="2026-01-01T00:00:00+00:00",
        code_commit=None,
        code_has_uncommitted_changes=None,
        runs_per_case=runs,
        limits=AnalyzerLimits(max_steps=40, max_cost_usd=0.5, max_submissions=3),
        max_evidence_judgements=30,
        analyzer=role("repo_analyzer", "vendor/analyzer"),
        judge=role("judge", "vendor/judge"),
        cases=[case_record(case)],
    )


@pytest.fixture
def stored_run(tmp_path) -> ResultStore:
    """A copy of the stored benchmark run of the fixtures, free to be written to."""
    folder = tmp_path / "example-run"
    shutil.copytree(FIXTURES / "results" / "example-run", folder)
    return ResultStore(folder)


@pytest.fixture
def example_cases():
    return load_cases(FIXTURES / "cases")


# --------------------------------------------------------------------- plan
def test_plan_counts_analyzer_runs_and_bounds_the_judge_calls(golden_case):
    planned = plan([golden_case], runs_per_case=3, max_evidence_judgements=30)
    assert planned.analyzer_runs == 3
    assert planned.judge_calls_at_most == 3 * (3 + 30)
    # The label cites evidence for three claims: the product and two features.
    assert planned.judge_calls_if_like_label == 3 * (3 + 3)


def test_plan_never_expects_more_evidence_questions_than_the_limit(golden_case):
    planned = plan([golden_case], runs_per_case=2, max_evidence_judgements=1)
    assert planned.judge_calls_if_like_label == planned.judge_calls_at_most == 2 * (3 + 1)


# ---------------------------------------------------------------------- run
def test_benchmark_runs_each_case_as_often_as_asked_and_stores_everything(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    drafted = profile(feature("sharing"))
    analyzer = scripted(submission(drafted), submission(drafted), model="vendor/analyzer")
    judge = scripted(MATCH, SUPPORTED, SUPPORTED, MATCH, SUPPORTED, SUPPORTED, model="vendor/judge")
    store = ResultStore(tmp_path / "results")
    said: list[str] = []
    scores = run_benchmark(
        [golden_case],
        config=config_for(golden_case, runs=2),
        analyzer=AnalyzerSetup(model=analyzer, extractors=[], limits=Limits()),
        judge=judge,
        store=store,
        live=True,
        progress=said.append,
    )
    assert [(run.case, run.run, run.ending) for run in scores.runs] == [
        ("notes", 1, RunEnding.PROFILE),
        ("notes", 2, RunEnding.PROFILE),
    ]
    first = scores.runs[0]
    assert first.profile is not None and first.profile.features is not None
    assert [m.id for m in first.profile.features.matching.missed] == ["pair-devices"]
    assert (first.judge_calls, first.judge_models) == (3, ["vendor/judge"])
    assert not first.judged_by_evaluated_model
    assert [run.run for run in store.read_runs()] == [1, 2]
    assert store.read_verdicts("notes", 2) is not None
    assert (store.folder / "excerpts" / "notes" / "run-1.json").exists()
    assert store.read_config().runs_per_case == 2
    assert any("judging" in line for line in said)


def test_benchmark_goes_on_after_a_run_whose_provider_failed(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    analyzer = scripted(LLMError("HTTP 503", status=503), submission(profile(feature("sharing"))))
    judge = scripted(MATCH, SUPPORTED, SUPPORTED)
    store = ResultStore(tmp_path / "results")
    scores = run_benchmark(
        [golden_case],
        config=config_for(golden_case, runs=2),
        analyzer=AnalyzerSetup(model=analyzer, extractors=[], limits=Limits()),
        judge=judge,
        store=store,
        live=True,
    )
    failed, done = scores.runs
    assert (failed.ending, failed.profile, failed.judge_calls) == (RunEnding.MODEL_FAILED, None, 0)
    assert done.ending is RunEnding.PROFILE
    assert store.read_verdicts("notes", 1) is None
    assert scores.cases[0].endings == {"model_failed": 1, "profile": 1}
    assert scores.cases[0].metrics["feature_recall"].counted == 1


def test_run_judged_by_the_model_that_drafted_it_is_marked(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    same = "vendor/fallback"
    scores = run_benchmark(
        [golden_case],
        config=config_for(golden_case, runs=1),
        analyzer=AnalyzerSetup(
            model=scripted(submission(profile(feature("sharing"))), model=same),
            extractors=[],
            limits=Limits(),
        ),
        judge=scripted(MATCH, SUPPORTED, SUPPORTED, model=same),
        store=ResultStore(tmp_path / "results"),
        live=True,
    )
    assert scores.runs[0].judged_by_evaluated_model
    assert "judged by the model that drafted it" in render(config_for(golden_case, 1), scores)


def on_another_route(config: BenchmarkConfig, requested_model: str | None) -> BenchmarkConfig:
    route = JudgeRoute(
        route="claude_code",
        spends="subscription usage",
        cost_known=False,
        requested_model=requested_model,
        role_settings_not_applied=["temperature"],
    )
    return config.model_copy(update={"judge_route": route})


def test_route_of_the_judge_is_recorded_and_its_cost_is_unknown_not_zero(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    config = on_another_route(config_for(golden_case, runs=1), None)
    store = ResultStore(tmp_path / "results")
    scores = run_benchmark(
        [golden_case],
        config=config,
        analyzer=AnalyzerSetup(
            model=scripted(submission(profile(feature("sharing")))), extractors=[], limits=Limits()
        ),
        judge=scripted(MATCH, SUPPORTED, SUPPORTED, cost=0.0, model="model-of-the-route"),
        store=store,
        live=True,
    )
    (score,) = scores.runs
    assert (score.judge_route, score.judge_cost_usd) == ("claude_code", None)
    assert score.judge_models == ["model-of-the-route"]
    assert scores.cases[0].metrics["judge_cost_usd"].counted == 0
    stored = store.read_verdicts("notes", 1)
    assert stored is not None and (stored.route, stored.cost_known) == ("claude_code", False)
    assert rescore([golden_case], store).scores == scores
    report = render(config, scores)
    assert "judge: via **claude_code**, model left to that route" in report
    assert "via claude_code, cost unknown, 3 calls" in report
    assert "settings of the judge role not applied: temperature" in report


def test_judge_on_another_route_is_compared_by_the_model_asked_of_it(
    golden_case, scripted, tmp_path
):
    config = on_another_route(config_for(golden_case, runs=1), "analyzer")
    with pytest.raises(SameModelError):
        run_benchmark(
            [golden_case],
            config=config,
            analyzer=AnalyzerSetup(model=scripted(), extractors=[], limits=Limits()),
            judge=scripted(),
            store=ResultStore(tmp_path / "results"),
            live=True,
        )


def test_run_judged_by_the_drafting_model_under_another_spelling_is_marked(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    scores = run_benchmark(
        [golden_case],
        config=on_another_route(config_for(golden_case, runs=1), None),
        analyzer=AnalyzerSetup(
            model=scripted(submission(profile(feature("sharing"))), model="vendor/model-5.5"),
            extractors=[],
            limits=Limits(),
        ),
        judge=scripted(MATCH, SUPPORTED, SUPPORTED, model="model-5-5"),
        store=ResultStore(tmp_path / "results"),
        live=True,
    )
    assert scores.runs[0].judged_by_evaluated_model


# ------------------------------------------------------- before it may spend
def test_benchmark_does_not_start_unless_told_it_may_spend(golden_case, scripted, tmp_path):
    analyzer, judge = scripted(), scripted()
    with pytest.raises(NotLive):
        run_benchmark(
            [golden_case],
            config=config_for(golden_case, runs=1),
            analyzer=AnalyzerSetup(model=analyzer, extractors=[], limits=Limits()),
            judge=judge,
            store=ResultStore(tmp_path / "results"),
            live=False,
        )
    assert analyzer.requests == judge.requests == []
    assert not (tmp_path / "results").exists()


def test_benchmark_does_not_start_with_a_judge_that_is_the_analyzers_model(
    golden_case, scripted, tmp_path
):
    config = config_for(golden_case, runs=1)
    config = config.model_copy(update={"judge": role("judge", config.analyzer.model)})
    analyzer = scripted()
    with pytest.raises(SameModelError):
        run_benchmark(
            [golden_case],
            config=config,
            analyzer=AnalyzerSetup(model=analyzer, extractors=[], limits=Limits()),
            judge=scripted(),
            store=ResultStore(tmp_path / "results"),
            live=True,
        )
    assert analyzer.requests == []
    assert not (tmp_path / "results").exists()


# ----------------------------------------------------- what is kept out of git
def test_reply_that_could_not_be_read_is_stored_apart_from_the_verdicts(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    quoted = "The lines say: share line 1, share line 2. So yes."
    store = ResultStore(tmp_path / "results")
    run_benchmark(
        [golden_case],
        config=config_for(golden_case, runs=1),
        analyzer=AnalyzerSetup(
            model=scripted(submission(profile(feature("sharing")))), extractors=[], limits=Limits()
        ),
        judge=scripted(MATCH, quoted, SUPPORTED),
        store=store,
        live=True,
    )
    verdicts = (store.folder / "verdicts" / "notes" / "run-1.json").read_text()
    assert "share line" not in verdicts
    assert '"problem": "the reply is not JSON"' in verdicts
    apart = (store.folder / "malformed_replies" / "notes" / "run-1.json").read_text()
    assert quoted in apart and "evidence:product" in apart


def test_git_ignores_excerpts_and_unread_replies_wherever_the_results_folder_is(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    checkout = tmp_path / "somewhere" / "else"
    store = ResultStore(checkout / "my-results" / "run")
    run_benchmark(
        [golden_case],
        config=config_for(golden_case, runs=1),
        analyzer=AnalyzerSetup(
            model=scripted(submission(profile(feature("sharing")))), extractors=[], limits=Limits()
        ),
        judge=scripted(MATCH, "unreadable", SUPPORTED),
        store=store,
        live=True,
    )
    assert (store.folder / "excerpts").is_dir() and (store.folder / "malformed_replies").is_dir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True, capture_output=True)
    seen = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=checkout, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert "raw/notes/run-1.json" in seen and "verdicts/notes/run-1.json" in seen
    assert "excerpts" not in seen and "malformed_replies" not in seen


# ------------------------------------------------------------- configuration
def test_code_version_of_a_folder_that_is_no_checkout_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert code_version(tmp_path) == (None, None)


def test_code_version_names_the_commit_and_says_when_there_are_changes(repository):
    repo, _ = repository
    commit, changed = code_version(repo)
    assert (len(commit or ""), changed) == (40, False)
    (repo / "new.txt").write_text("x")
    assert code_version(repo) == (commit, True)


def test_configuration_records_the_cases_the_limits_and_both_roles(golden_case, tmp_path):
    config = benchmark_config(
        [golden_case],
        runs_per_case=2,
        limits=Limits(max_steps=7),
        max_evidence_judgements=4,
        analyzer=role("repo_analyzer", "vendor/analyzer"),
        judge=role("judge", "vendor/judge"),
        checkout=tmp_path,
    )
    assert (config.runs_per_case, config.limits.max_steps, config.max_evidence_judgements) == (
        2,
        7,
        4,
    )
    assert [case.name for case in config.cases] == ["notes"]
    assert (config.analyzer.model, config.judge.model) == ("vendor/analyzer", "vendor/judge")
    assert config.benchmark_id.endswith("Z")


# ------------------------------------------------------------------ rescore
def test_scoring_again_from_the_stored_run_gives_the_same_scores_without_a_model(
    golden_case, scripted, submission, profile, feature, tmp_path
):
    store = ResultStore(tmp_path / "results")
    scores = run_benchmark(
        [golden_case],
        config=config_for(golden_case, runs=2),
        analyzer=AnalyzerSetup(
            model=scripted(
                submission(profile(feature("sharing"))), LLMError("HTTP 500", status=500)
            ),
            extractors=[],
            limits=Limits(),
        ),
        judge=scripted("not what was asked for", SUPPORTED, SUPPORTED),
        store=store,
        live=True,
    )
    shutil.rmtree(store.folder / "excerpts")  # what git ignores is not needed to score again
    again = rescore([golden_case], store)
    assert again.scores == scores
    assert again.scores.runs[0].profile is not None
    assert again.scores.runs[0].profile.feature_matching_state is VerdictState.MALFORMED
    assert (again.changed_labels, again.without_a_case) == ((), ())


def test_stored_run_of_the_fixtures_is_scored(stored_run, example_cases):
    rescored = rescore(example_cases, stored_run)
    first, second = rescored.scores.runs
    assert first.profile is not None and first.profile.features is not None
    features = first.profile.features
    assert [
        (m.drafted_id, m.expected_status.value) for m in features.live_but_expected_not_live
    ] == [("cloud-sync", "unreleased")]
    assert features.live_and_not_in_label == ["dark-mode"]
    assert [m.id for m in features.matching.missed] == ["share-notes"]
    assert features.matching.not_in_label == ["dark-mode"]
    assert features.recall.ratio == pytest.approx(2 / 3)
    assert features.precision is None
    evidence = first.profile.evidence
    assert evidence.supported.model_dump() == {"count": 1, "of": 2, "ratio": 0.5}
    assert [(o.claim, o.reason) for o in evidence.not_supported] == [
        ("feature:editor", "an empty type")
    ]
    assert evidence.without_usable_verdict == ["feature:cloud-sync"]
    assert (first.judge_calls, first.judge_cost_usd) == (5, pytest.approx(0.1))
    assert (second.ending, second.profile, second.cost_usd) == (RunEnding.NO_PROFILE, None, 0.05)
    summary = rescored.scores.cases[0]
    assert summary.live_but_expected_not_live_in_any_run == ["cloud-sync"]
    assert summary.live_and_not_in_label_in_any_run == ["dark-mode"]
    assert summary.metrics["features_live_but_expected_not_live"].values == [1.0, None]
    assert summary.metrics["feature_precision_exhaustive_label_only"].counted == 0
    assert summary.metrics["analyzer_cost_usd"].median == pytest.approx(0.031)


def test_scoring_again_says_when_a_label_has_changed(stored_run, example_cases, tmp_path):
    cases_dir = tmp_path / "cases"
    shutil.copytree(FIXTURES / "cases", cases_dir)
    label = cases_dir / "example" / "expected_profile.json"
    label.write_text(label.read_text().replace('"unreleased"', '"live"'))
    rescored = rescore(load_cases(cases_dir), stored_run)
    assert rescored.changed_labels == ("example",)
    features = rescored.scores.runs[0].profile.features  # type: ignore[union-attr]
    assert features.live_but_expected_not_live == []  # type: ignore[union-attr]


def test_scoring_again_takes_what_the_case_says_about_its_label_now(
    stored_run, example_cases, tmp_path
):
    cases_dir = tmp_path / "cases"
    shutil.copytree(FIXTURES / "cases", cases_dir)
    described = cases_dir / "example" / "case.yaml"
    described.write_text(described.read_text() + "label_is_exhaustive: true\n")
    rescored = rescore(load_cases(cases_dir), stored_run)
    assert rescored.changed_labels == ()
    assert rescored.config.cases[0].label_is_exhaustive
    features = rescored.scores.runs[0].profile.features  # type: ignore[union-attr]
    assert features.precision.ratio == pytest.approx(2 / 3)  # type: ignore[union-attr]


def test_scoring_again_leaves_the_stored_drafts_and_verdicts_as_they_were(
    stored_run, example_cases
):
    kept = ["config.json", "raw/example/run-1.json", "raw/example/run-2.json"]
    kept.append("verdicts/example/run-1.json")
    before = {name: (stored_run.folder / name).read_bytes() for name in kept}
    rescore(example_cases, stored_run, checkout=stored_run.folder)
    assert {name: (stored_run.folder / name).read_bytes() for name in kept} == before


def test_scoring_again_records_when_and_by_which_code(stored_run, example_cases, repository):
    repo, _ = repository
    again = rescore(example_cases, stored_run, checkout=repo).scores.rescored
    assert again is not None
    assert len(again.code_commit or "") == 40 and again.at.endswith("+00:00")
    assert rescore(example_cases, stored_run).scores.rescored is None


def test_scoring_again_leaves_out_runs_of_a_case_that_was_not_given(stored_run, golden_case):
    rescored = rescore([golden_case], stored_run)
    assert rescored.without_a_case == ("example",)
    assert rescored.scores.runs == []


def test_run_with_a_profile_and_no_verdicts_is_scored_on_what_needs_no_judge(
    stored_run, example_cases
):
    (stored_run.folder / "verdicts" / "example" / "run-1.json").unlink()
    score = rescore(example_cases, stored_run).scores.runs[0]
    assert score.profile is not None
    assert score.profile.features is None
    assert score.profile.product.name_matches
    assert score.profile.evidence.claims_with_evidence.ratio == 0.6


def test_folder_that_is_not_a_stored_run_is_refused(tmp_path, example_cases):
    with pytest.raises(ResultsError, match="config.json is missing"):
        rescore(example_cases, ResultStore(tmp_path))


def test_stored_file_this_version_cannot_read_is_refused(stored_run, example_cases):
    (stored_run.folder / "raw" / "example" / "run-2.json").write_text('{"case": "example"}')
    with pytest.raises(ResultsError, match="run-2.json is not a AnalyzerRun"):
        rescore(example_cases, stored_run)
