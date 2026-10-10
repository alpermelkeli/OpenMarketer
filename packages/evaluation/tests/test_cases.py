"""Tests for loading golden cases, and for the cases this repository ships."""

from pathlib import Path

import pytest

from openmarketer_evaluation.cases import CaseError, LabelProvenance, load_case, load_cases

COMMIT = "849bbfb6358417e576fc8e499baeb88482f2a46a"
PROFILE = '{"product": {"name": "Example App", "type": "dev_tool"}}'
SHIPPED_CASES = Path(__file__).resolve().parents[3] / "evals" / "cases"


def write_case(folder: Path, *, profile: str | None = PROFILE, **fields: str | None) -> Path:
    described = {
        "repository": "https://example.com/acme/app",
        "commit": COMMIT,
        "label_provenance": "written_by_person",
        **fields,
    }
    folder.mkdir(parents=True)
    lines = [f"{key}: {value}" for key, value in described.items() if value is not None]
    (folder / "case.yaml").write_text("\n".join(lines) + "\n")
    if profile is not None:
        (folder / "expected_profile.json").write_text(profile)
    return folder


def test_case_is_loaded_with_its_pinned_commit_and_expected_profile(tmp_path):
    case = load_case(write_case(tmp_path / "example", notes="checked by hand"))
    assert (case.name, case.repository, case.commit) == (
        "example",
        "https://example.com/acme/app",
        COMMIT,
    )
    assert case.label_provenance is LabelProvenance.WRITTEN_BY_PERSON
    assert case.notes == "checked by hand"
    assert case.expected.product.name == "Example App"
    assert len(case.expected_sha256) == 64


@pytest.mark.parametrize("commit", ["1" * 40, "12345e" + "6" * 34])
def test_commit_id_that_looks_like_a_number_is_read_as_text(tmp_path, commit):
    assert load_case(write_case(tmp_path / "example", commit=commit)).commit == commit


def test_case_without_a_commit_is_refused(tmp_path):
    with pytest.raises(CaseError, match="commit"):
        load_case(write_case(tmp_path / "example", commit=None))


@pytest.mark.parametrize("commit", ["main", "849bbfb", COMMIT.upper(), "HEAD"])
def test_case_pinned_to_something_that_is_not_a_full_commit_id_is_refused(tmp_path, commit):
    with pytest.raises(CaseError, match="full commit id"):
        load_case(write_case(tmp_path / "example", commit=commit))


def test_case_with_an_unknown_provenance_is_refused(tmp_path):
    with pytest.raises(CaseError, match="label_provenance"):
        load_case(write_case(tmp_path / "example", label_provenance="trust_me"))


def test_case_without_a_provenance_is_refused(tmp_path):
    with pytest.raises(CaseError, match="label_provenance"):
        load_case(write_case(tmp_path / "example", label_provenance=None))


def test_expected_profile_that_does_not_match_the_schema_is_refused(tmp_path):
    folder = write_case(tmp_path / "example", profile='{"product": {"name": "Example App"}}')
    with pytest.raises(CaseError, match=r"expected_profile\.json: product\.type"):
        load_case(folder)


def test_expected_profile_with_an_unknown_field_is_refused(tmp_path):
    profile = '{"product": {"name": "A", "type": "dev_tool"}, "approved": true}'
    with pytest.raises(CaseError, match="approved"):
        load_case(write_case(tmp_path / "example", profile=profile))


def test_case_without_an_expected_profile_is_refused(tmp_path):
    with pytest.raises(CaseError, match=r"expected_profile\.json is missing"):
        load_case(write_case(tmp_path / "example", profile=None))


@pytest.mark.parametrize(
    "repository", ["/home/someone/app", "file:///srv/app", "http://example.com/acme/app"]
)
def test_case_that_is_not_a_remote_https_repository_is_refused(tmp_path, repository):
    with pytest.raises(CaseError, match="repository"):
        load_case(write_case(tmp_path / "example", repository=repository))


def test_unknown_field_in_the_case_file_is_refused(tmp_path):
    with pytest.raises(CaseError, match="branch"):
        load_case(write_case(tmp_path / "example", branch="main"))


def test_all_cases_are_loaded_in_name_order(tmp_path):
    for name in ("zeta", "alpha"):
        write_case(tmp_path / name)
    assert [case.name for case in load_cases(tmp_path)] == ["alpha", "zeta"]


def test_only_the_cases_asked_for_are_loaded(tmp_path):
    for name in ("zeta", "alpha"):
        write_case(tmp_path / name)
    assert [case.name for case in load_cases(tmp_path, ["zeta"])] == ["zeta"]


def test_asking_for_a_case_that_is_not_there_names_the_ones_that_are(tmp_path):
    write_case(tmp_path / "alpha")
    with pytest.raises(CaseError, match="unknown case beta; there is: alpha"):
        load_cases(tmp_path, ["beta"])


def test_folder_without_cases_is_refused(tmp_path):
    with pytest.raises(CaseError, match="no cases"):
        load_cases(tmp_path)


def test_label_is_not_exhaustive_unless_the_case_says_so(tmp_path):
    assert not load_case(write_case(tmp_path / "a")).label_is_exhaustive
    assert load_case(write_case(tmp_path / "b", label_is_exhaustive="true")).label_is_exhaustive
    assert not load_case(
        write_case(tmp_path / "c", label_is_exhaustive="false")
    ).label_is_exhaustive


def test_exhaustive_that_is_neither_yes_nor_no_is_refused(tmp_path):
    with pytest.raises(CaseError, match="label_is_exhaustive"):
        load_case(write_case(tmp_path / "example", label_is_exhaustive="mostly"))


def test_the_golden_set_is_the_one_shipped_case():
    (case,) = load_cases(SHIPPED_CASES)
    assert case.name == "excalidraw"
    assert case.notes


def test_label_written_by_an_assistant_is_not_passed_off_as_a_persons():
    case = load_case(SHIPPED_CASES / "excalidraw")
    assert case.label_provenance is LabelProvenance.WRITTEN_BY_AI_ASSISTANT_UNREVIEWED
    assert "NOT a human label" in case.notes
    assert "same model family as the judge" in case.notes


def test_shipped_label_does_not_claim_to_be_exhaustive_and_says_so():
    case = load_case(SHIPPED_CASES / "excalidraw")
    assert not case.label_is_exhaustive
    assert "NOT exhaustive" in case.notes


def test_shipped_label_has_features_that_are_not_live():
    case = load_case(SHIPPED_CASES / "excalidraw")
    assert {feature.status.value for feature in case.expected.features} == {"live", "unreleased"}
