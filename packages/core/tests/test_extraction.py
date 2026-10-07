"""Tests for the extractor interface and runner."""

import pytest
from pydantic import ValidationError

from openmarketer_core.extraction import Extractor, Fact, discover_extractors, run_extractors
from openmarketer_core.intake import RepoFiles


class Readme:
    name = "readme"

    def extract(self, files):
        yield Fact(
            kind="readme.title", value=files.read_text("README.md").strip(), file="README.md"
        )


class Broken:
    name = "broken"

    def extract(self, files):
        yield Fact(kind="broken.first", value=1, file="README.md")
        raise RuntimeError("boom")


@pytest.fixture
def files(tmp_path):
    (tmp_path / "README.md").write_text("# Example\n")
    return RepoFiles(tmp_path)


def test_facts_are_tagged_with_their_extractor(files):
    result = run_extractors(files, [Readme()])
    assert [(f.extractor, f.kind, f.value) for f in result.facts] == [
        ("readme", "readme.title", "# Example")
    ]
    assert result.errors == {}


def test_a_failing_extractor_does_not_stop_the_others(files):
    result = run_extractors(files, [Broken(), Readme()])
    assert [f.extractor for f in result.facts] == ["readme"]  # nothing partial from "broken"
    assert result.errors == {"broken": "RuntimeError: boom"}


def test_classes_satisfy_the_protocol():
    assert isinstance(Readme(), Extractor)
    assert not isinstance(object(), Extractor)


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "Not A Kind"},
        {"start_line": 0},
        {"end_line": 5},
        {"start_line": 9, "end_line": 3},
        {"unknown": 1},
    ],
)
def test_invalid_facts_are_rejected(fields):
    with pytest.raises(ValidationError):
        Fact(**{"kind": "a.b", "value": 1, "file": "x", **fields})


def test_installed_extractors_are_discovered():
    assert "package_json" in [e.name for e in discover_extractors()]
