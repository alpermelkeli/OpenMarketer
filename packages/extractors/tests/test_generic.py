"""Tests for the stack-independent extractor."""

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.generic import GenericExtractor

README = """\
<p align="center"><img src="logo.png"></p>

[![CI](https://example.com/badge.svg)](https://example.com)

# Tidepool

Tidepool is a tiny tide-table app
for sailors.

## Install

```
# not a heading
cargo install tidepool
```
"""


def extract(tmp_path, files: dict[str, str]):
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return list(GenericExtractor().extract(RepoFiles(tmp_path)))


def values(facts, kind):
    return [f.value for f in facts if f.kind == kind]


def test_unknown_stack_still_yields_an_overview(tmp_path):
    facts = extract(
        tmp_path,
        {
            "README.md": README,
            "LICENSE": "MIT",
            "Cargo.toml": '[package]\nname = "tidepool"\n',
            "src/main.rs": "fn main() {}\n",
            "src/tides.rs": "",
            "scripts/release.sh": "",
        },
    )
    assert values(facts, "repo.language") == [
        {"name": "Rust", "files": 2},
        {"name": "Shell", "files": 1},
    ]
    assert values(facts, "repo.license") == ["LICENSE"]
    assert [(f.value, f.file) for f in facts if f.kind == "repo.manifest"] == [
        ({"ecosystem": "cargo"}, "Cargo.toml")
    ]


def test_readme_title_and_first_paragraph(tmp_path):
    facts = extract(tmp_path, {"README.md": README})
    readme = next(f for f in facts if f.kind == "repo.readme")
    assert readme.value == {
        "title": "Tidepool",
        "summary": "Tidepool is a tiny tide-table app for sailors.",
    }
    assert (readme.file, readme.start_line) == ("README.md", 5)


def test_readme_without_heading(tmp_path):
    facts = extract(tmp_path, {"readme.txt": "Just a line about the project.\n\nMore.\n"})
    assert values(facts, "repo.readme") == [
        {"title": "", "summary": "Just a line about the project."}
    ]


def test_readme_with_html_header(tmp_path):
    readme = """\
<h1 align="center">
  <img src="logo.png" width="80"><br>
  Trail Mate
</h1>
<!-- a comment
     over two lines -->
<p align="center">Plan hikes <b>offline</b>.</p>

## Usage
"""
    facts = extract(tmp_path, {"README.md": readme})
    fact = next(f for f in facts if f.kind == "repo.readme")
    assert fact.value == {"title": "Trail Mate", "summary": "Plan hikes offline."}
    assert fact.start_line == 1


def test_plain_readme_is_preferred_over_variants(tmp_path):
    facts = extract(tmp_path, {"README-project.md": "# Template\n", "README.md": "# Real\n"})
    assert values(facts, "repo.readme") == [{"title": "Real", "summary": ""}]


def test_only_the_root_readme_is_used(tmp_path):
    facts = extract(tmp_path, {"docs/README.md": "# Docs\n"})
    assert values(facts, "repo.readme") == []


def test_manifests_closest_to_the_root_come_first(tmp_path):
    facts = extract(
        tmp_path,
        {
            "apps/mobile/pubspec.yaml": "name: m\n",
            "go.mod": "module x\n",
            "services/api/MyApi.csproj": "<Project/>",
            "node_modules/x/package.json": "{}",
        },
    )
    assert [(f.file, f.value) for f in facts if f.kind == "repo.manifest"] == [
        ("go.mod", {"ecosystem": "go"}),
        ("apps/mobile/pubspec.yaml", {"ecosystem": "flutter"}),
        ("services/api/MyApi.csproj", {"ecosystem": "dotnet"}),
    ]


def test_empty_repository_yields_nothing(tmp_path):
    assert extract(tmp_path, {}) == []
