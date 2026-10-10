"""Tests for summarising runs and for the report a person reads."""

import shutil
from pathlib import Path

from openmarketer_evaluation.benchmark import rescore
from openmarketer_evaluation.cases import LabelProvenance, load_cases
from openmarketer_evaluation.report import BenchmarkScores, limits_of, render, summarise, summary
from openmarketer_evaluation.results import ResultStore

FIXTURES = Path(__file__).parent / "fixtures"


def test_summary_gives_every_run_and_min_median_max():
    summarised = summary([0.5, 1.0, 0.75])
    assert summarised.values == [0.5, 1.0, 0.75]
    assert (summarised.runs, summarised.counted) == (3, 3)
    assert (summarised.min, summarised.median, summarised.max) == (0.5, 0.75, 1.0)


def test_run_without_a_value_is_left_out_and_counted_as_left_out():
    summarised = summary([None, 0.25, None])
    assert (summarised.runs, summarised.counted) == (3, 1)
    assert (summarised.min, summarised.median, summarised.max) == (0.25, 0.25, 0.25)


def test_summary_of_no_value_is_not_a_number():
    summarised = summary([None, None])
    assert (summarised.counted, summarised.min, summarised.median, summarised.max) == (
        0,
        None,
        None,
        None,
    )


def rescored():
    return rescore(
        load_cases(FIXTURES / "cases"), ResultStore(FIXTURES / "results" / "example-run")
    )


def test_report_shows_the_provenance_of_the_label_next_to_the_scores():
    done = rescored()
    report = render(done.config, done.scores)
    case_section = report[report.index("## example") :]
    assert case_section.index("label: **written_by_person**") < case_section.index("| Metric |")


def test_report_says_how_many_runs_each_number_comes_from():
    done = rescored()
    report = render(done.config, done.scores)
    assert "| feature_recall | 1 of 2 | 0.667 | 0.667 | 0.667 | 0.667, - |" in report
    assert "| analyzer_cost_usd | 2 of 2 | 0.012 | 0.031 | 0.05 | 0.012, 0.05 |" in report


def test_report_gives_the_costly_error_a_line_of_its_own():
    done = rescored()
    report = render(done.config, done.scores)
    assert (
        "- live in the draft, not live in the label, in any run (the costly error): "
        "**cloud-sync**" in report
    )
    assert (
        "- **live in the draft, not live in the label (the costly error): "
        "cloud-sync (label: sync-notes is unreleased)**" in report
    )


def test_report_does_not_call_a_feature_outside_an_open_label_an_error():
    done = rescored()
    report = render(done.config, done.scores)
    assert "in any run (to look at, not errors): dark-mode" in report
    assert (
        "- not in the label, which is not exhaustive (to look at, not errors; no precision): "
        "dark-mode" in report
    )
    assert "invented" not in report.split("## example")[1]
    assert "| feature_precision_exhaustive_label_only | 0 of 2 | - | - | - | -, - |" in report


def test_report_calls_it_invented_only_against_an_exhaustive_label(tmp_path):
    shutil.copytree(FIXTURES / "cases", tmp_path / "cases")
    described = tmp_path / "cases" / "example" / "case.yaml"
    described.write_text(described.read_text() + "label_is_exhaustive: true\n")
    done = rescore(
        load_cases(tmp_path / "cases"), ResultStore(FIXTURES / "results" / "example-run")
    )
    report = render(done.config, done.scores)
    assert "**exhaustive**" in report
    assert "- precision 2 of 3 (0.667)" in report
    assert "- invented (the label is exhaustive): dark-mode" in report
    assert "the label is exhaustive, so these are invented" in report


def test_report_shows_three_outcomes_of_evidence_with_the_judges_reasons():
    done = rescored()
    report = render(done.config, done.scores)
    assert "- evidence: 1 supported, 0 partly supported, 1 not supported; " in report
    assert "strictly supported 1 of 2 (0.5), at least partly 1 of 2 (0.5)" in report
    assert "  - feature:editor: an empty type" in report
    assert "- evidence with a malformed or failed verdict: feature:cloud-sync" in report


def test_report_shows_the_statuses_of_drafted_and_expected_features_side_by_side():
    done = rescored()
    report = render(done.config, done.scores)
    assert (
        "- feature statuses: drafted 3 live, 0 unreleased, 0 unknown; "
        "expected 2 live, 1 unreleased, 0 unknown" in report
    )
    assert "- **every drafted feature is live (3 of 3)**" in report


def test_report_names_a_missed_feature_with_its_status_in_the_label():
    done = rescored()
    assert "- missed: share-notes (live in the label)" in render(done.config, done.scores)


def test_limits_are_said_of_the_cases_the_run_has():
    done = rescored()
    limits = done.scores.limits_of_this_result
    assert limits[0].startswith("The golden set is one repository (example).")
    assert "The label of example was written by a person, who can be wrong too." in limits
    assert any("does not list every feature" in limit for limit in limits)
    assert any(limit.startswith("With 2 runs per case") for limit in limits)
    one_run = limits_of(done.config.model_copy(update={"runs_per_case": 1}))
    assert any(limit.startswith("With one run per case") for limit in one_run)


def test_limits_of_an_unreviewed_label_say_that_it_is_unreviewed():
    done = rescored()
    unreviewed = LabelProvenance.WRITTEN_BY_AI_ASSISTANT_UNREVIEWED
    cases = [c.model_copy(update={"label_provenance": unreviewed}) for c in done.config.cases]
    limits = limits_of(done.config.model_copy(update={"cases": cases}))
    assert any("has not been reviewed by a person" in limit for limit in limits)
    assert not any("analyzer's own draft" in limit for limit in limits)


def test_report_states_the_limits_of_the_result_and_the_models():
    done = rescored()
    report = render(done.config, done.scores)
    assert "## Limits of this result" in report
    assert "The golden set is one repository (example)" in report
    assert "analyzer: vendor/analyzer" in report and "judge: vendor/judge" in report


def test_report_records_a_run_without_a_profile_as_a_result():
    done = rescored()
    assert "### Run 2: no_profile" in render(done.config, done.scores)
    assert "- no profile: no profile after 40 steps" in render(done.config, done.scores)


def test_scores_file_survives_being_written_and_read():
    scores = rescored().scores
    assert BenchmarkScores.model_validate_json(scores.model_dump_json()) == scores
    assert scores.limits_of_this_result


def test_report_says_when_a_label_was_written_by_an_assistant_and_not_reviewed():
    done = rescored()
    unreviewed = LabelProvenance.WRITTEN_BY_AI_ASSISTANT_UNREVIEWED
    config = done.config.model_copy(
        update={
            "cases": [
                c.model_copy(update={"label_provenance": unreviewed}) for c in done.config.cases
            ]
        }
    )
    report = render(config, summarise(config, done.scores.runs))
    assert "label: **written_by_ai_assistant_unreviewed**" in report
    assert "not reviewed by a person: not a human label" in report


def test_report_says_what_the_files_of_a_run_hold():
    done = rescored()
    report = render(done.config, done.scores)
    assert "## About the files of this run" in report
    assert "the judge's own short sentence about a public repository" in report
