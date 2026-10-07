"""Tests for the extractor interface and runner."""

import pytest
from pydantic import ValidationError

from openmarketer_core.extraction import (
    ExtractedFact,
    Extractor,
    Fact,
    discover_extractors,
    group_by_scope,
    is_auxiliary,
    run_extractors,
)
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


# ------------------------------------------------------------------ scopes
def fact(kind: str, value, file: str) -> ExtractedFact:
    return ExtractedFact(extractor="t", kind=kind, value=value, file=file)


def manifest(file: str) -> ExtractedFact:
    return fact("repo.manifest", {"ecosystem": "x"}, file)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.ts", False),
        ("apps/mobile/package.json", False),
        ("docs/package.json", True),
        ("tests/fixtures/go.mod", True),
        ("Examples/demo/pubspec.yaml", True),
        ("docs", False),  # a file named docs at the root is not inside a docs folder
    ],
)
def test_auxiliary_paths(path, expected):
    assert is_auxiliary(path) is expected


def test_monorepo_projects_are_kept_apart():
    scopes = group_by_scope(
        [
            fact("repo.readme", {"title": "Mono"}, "README.md"),
            manifest("package.json"),
            manifest("docs/package.json"),
            manifest("apps/mobile/package.json"),
            manifest("cli/package.json"),
            fact("manifest.name", "docs-site", "docs/package.json"),
            fact("manifest.name", "mobile", "apps/mobile/package.json"),
            fact("manifest.platform", "ios", "apps/mobile/app.json"),
            fact("manifest.name", "cli", "cli/package.json"),
            fact("manifest.platform", "cli", "cli/package.json"),
            fact(
                "android.permission",
                "CAMERA",
                "apps/mobile/android/app/src/main/AndroidManifest.xml",
            ),
        ]
    )
    assert [(s.path, s.auxiliary) for s in scopes] == [
        ("", False),
        ("cli", False),
        ("apps/mobile", False),
        ("docs", True),
    ]
    mobile = next(s for s in scopes if s.path == "apps/mobile")
    assert [(f.kind, f.value) for f in mobile.facts if f.kind != "repo.manifest"] == [
        ("manifest.name", "mobile"),
        ("manifest.platform", "ios"),
        ("android.permission", "CAMERA"),
    ]


def test_xcode_project_belongs_to_the_folder_around_it():
    scopes = group_by_scope(
        [
            manifest("iosApp/iosApp.xcodeproj/project.pbxproj"),
            fact("ios.permission", {"key": "NSCameraUsageDescription"}, "iosApp/iosApp/Info.plist"),
        ]
    )
    assert [(s.path, len(s.facts)) for s in scopes] == [("iosApp", 2)]


def test_facts_without_any_manifest_fall_into_the_root_scope():
    scopes = group_by_scope([fact("repo.language", {"name": "Go"}, "cmd/x/main.go")])
    assert [(s.path, s.auxiliary, len(s.facts)) for s in scopes] == [("", False, 1)]
