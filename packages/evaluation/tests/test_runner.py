"""Tests for running the analyzer on a case: a scripted model on a local repository."""

import shutil

import pytest

from openmarketer_core.llm import LLMError
from openmarketer_core.profile import Evidence
from openmarketer_core.repository_analysis.analyzer_agent import Limits
from openmarketer_core.repository_analysis.intake import RepoFiles
from openmarketer_evaluation import runner
from openmarketer_evaluation.results import RunEnding
from openmarketer_evaluation.runner import AnalyzerSetup, cited_claims, excerpt, run_analyzer

needs_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="needs gitleaks")


def setup(model, **limits) -> AnalyzerSetup:
    return AnalyzerSetup(model=model, extractors=[], limits=Limits(**limits))


def read_then_submit(submission, drafted):
    read = [
        {"id": "r1", "type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "README.md"}'}}
    ]  # fmt: skip
    return read, submission(drafted)


@needs_gitleaks
def test_run_analyses_the_pinned_commit_not_the_branch(
    golden_case, scripted, submission, profile, feature
):
    model = scripted(*read_then_submit(submission, profile(feature("sharing"))))
    run_analyzer(golden_case, 1, setup(model))
    readme = model.requests[1]["messages"][-1]["content"]
    assert "# Notes" in readme
    assert "Renamed since" not in readme


@needs_gitleaks
def test_run_records_the_draft_and_what_it_cost(
    golden_case, scripted, submission, profile, feature
):
    drafted = profile(feature("sharing"))
    model = scripted(*read_then_submit(submission, drafted), cost=0.02)
    run, _ = run_analyzer(golden_case, 3, setup(model))
    assert (run.case, run.run, run.commit) == ("notes", 3, golden_case.commit)
    assert (run.ending, run.error, run.profile) == (RunEnding.PROFILE, None, drafted)
    assert (run.model, run.model_calls, run.steps) == ("scripted/model", 2, 2)
    assert run.cost_usd == pytest.approx(0.04)
    assert run.tool_calls == ["read_file", "submit_profile"]


@needs_gitleaks
def test_run_keeps_the_lines_the_draft_cites(golden_case, scripted, submission, profile, feature):
    model = scripted(submission(profile(feature("sharing"))))
    _, cited = run_analyzer(golden_case, 1, setup(model))
    assert [claim.key for claim in cited.claims] == ["product", "feature:sharing"]
    product, sharing = cited.claims
    assert product.excerpts[0].text == "1: # Notes\n2: \n3: Share memories offline."
    assert sharing.excerpts[0].text == "1: share line 1\n2: share line 2\n3: share line 3"
    assert sharing.excerpts[0].complete


@needs_gitleaks
def test_run_that_reaches_its_step_limit_is_a_result(golden_case, scripted):
    listing = [
        {"id": "l", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}
    ]
    not_a_profile = [
        {"id": "s", "type": "function", "function": {"name": "submit_profile", "arguments": "{}"}}
    ]
    model = scripted(listing, not_a_profile, not_a_profile)
    run, cited = run_analyzer(golden_case, 1, setup(model, max_steps=3))
    assert (run.ending, run.profile, run.steps) == (RunEnding.NO_PROFILE, None, None)
    assert run.error
    assert run.model_calls == len(model.requests) > 0
    assert run.cost_usd == pytest.approx(0.01 * len(model.requests))
    assert cited.claims == []


@needs_gitleaks
def test_run_whose_model_provider_fails_is_a_result(golden_case, scripted):
    model = scripted(LLMError("the model provider answered HTTP 402", status=402))
    run, _ = run_analyzer(golden_case, 1, setup(model))
    assert (run.ending, run.error) == (
        RunEnding.MODEL_FAILED,
        "the model provider answered HTTP 402",
    )
    assert (run.model, run.model_calls, run.cost_usd) == (None, 0, 0.0)


def test_run_whose_commit_cannot_be_fetched_is_a_result_and_calls_no_model(golden_case, scripted):
    from dataclasses import replace

    model = scripted()
    run, _ = run_analyzer(replace(golden_case, commit="0123456789" * 4), 1, setup(model))
    assert run.ending is RunEnding.CLONE_FAILED
    assert "commit could not be fetched" in (run.error or "")
    assert model.requests == []


def test_error_of_a_run_does_not_name_the_temporary_folder(golden_case, scripted, monkeypatch):
    from openmarketer_core.repository_analysis.intake import IntakeError

    def refuse(source, clone_into, **_):
        raise IntakeError(f"destination is not empty: {clone_into}")

    monkeypatch.setattr(runner, "analyze_repository", refuse)
    run, _ = run_analyzer(golden_case, 1, setup(scripted()))
    assert run.error == "destination is not empty: <clone>/repo"


def test_defect_is_not_recorded_as_a_result(golden_case, scripted, monkeypatch):
    def broken(*_, **__):
        raise RuntimeError("a bug")

    monkeypatch.setattr(runner, "analyze_repository", broken)
    with pytest.raises(RuntimeError):
        run_analyzer(golden_case, 1, setup(scripted()))


# ----------------------------------------------------------------- excerpts
@pytest.fixture
def files(tmp_path) -> RepoFiles:
    (tmp_path / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 501)) + "\n")
    (tmp_path / "dense.txt").write_text("\n".join("y" * 60 for _ in range(400)) + "\n")
    (tmp_path / "wide.txt").write_text("x" * 5000 + "\n")
    (tmp_path / ".env").write_text("TOKEN=abc\n")
    return RepoFiles(tmp_path)


def test_excerpt_is_the_cited_lines_with_their_numbers(files):
    taken = excerpt(files, Evidence(file="long.txt", lines="10-12"), max_chars=1000)
    assert (taken.text, taken.complete) == ("10: line 10\n11: line 11\n12: line 12", True)


def test_excerpt_of_many_lines_is_cut_and_says_so(files):
    taken = excerpt(files, Evidence(file="long.txt", lines="1-400"), max_chars=100_000)
    assert len(taken.text.splitlines()) == runner.MAX_EXCERPT_LINES
    assert not taken.complete


def test_excerpt_without_a_line_range_is_the_head_of_the_file(files):
    taken = excerpt(files, Evidence(file="long.txt"), max_chars=100_000)
    assert taken.text.startswith("1: line 1\n")
    assert not taken.complete


def test_long_line_is_cut(files):
    taken = excerpt(files, Evidence(file="wide.txt", lines="1"), max_chars=100_000)
    assert len(taken.text) == len("1: ") + runner.MAX_LINE_CHARS


def test_excerpt_of_a_file_that_may_not_be_read_is_empty_and_incomplete(files):
    for path in (".env", "missing.txt"):
        taken = excerpt(files, Evidence(file=path, lines="1"), max_chars=1000)
        assert (taken.text, taken.complete) == ("", False)


def test_evidence_of_one_claim_shares_one_size_limit(files, profile, feature):
    cited = [{"file": "dense.txt", "lines": f"{start}-{start + 79}"} for start in range(1, 400, 80)]
    drafted = profile(feature("sharing", evidence=cited), product={"evidence": []})
    (claim,) = cited_claims(files, drafted)
    assert len(claim.excerpts) == 5
    assert sum(len(taken.text) for taken in claim.excerpts) <= runner.MAX_CLAIM_CHARS
    assert not claim.excerpts[-1].complete


def test_claim_without_evidence_is_not_kept_for_the_judge(files, profile, feature):
    drafted = profile(feature("sharing", evidence=[]), product={"evidence": []})
    assert cited_claims(files, drafted) == []
