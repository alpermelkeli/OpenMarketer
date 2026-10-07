"""Tests for the Cargo, pyproject and go.mod extractors."""

import pytest

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.manifests import CargoExtractor, GoModExtractor, PyprojectExtractor

CARGO = """\
[package]
name = "tidepool"
version = "0.4.2"
description = "Tide tables in your terminal"
homepage = "https://tidepool.example"
keywords = ["tides", "cli"]

[dependencies]
clap = "4"
"""

PYPROJECT = """\
[project]
name = "ledgerly"
version = "1.3.0"
description = "Bookkeeping API for freelancers"
keywords = ["invoices"]
dependencies = [
    "FastAPI>=0.115",
    "sqlalchemy[asyncio]>=2 ; python_version >= '3.12'",
]

[project.urls]
Homepage = "https://ledgerly.example"

[project.scripts]
ledgerly = "ledgerly.cli:main"

[tool.ruff]
line-length = 100
"""


def write(tmp_path, files: dict[str, str]) -> RepoFiles:
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return RepoFiles(tmp_path)


def found(facts, kind):
    return [(f.value, f.start_line) for f in facts if f.kind == kind]


# ------------------------------------------------------------------ cargo
def test_rust_command_line_tool(tmp_path):
    files = write(tmp_path, {"Cargo.toml": CARGO, "src/main.rs": "fn main() {}"})
    facts = list(CargoExtractor().extract(files))
    assert found(facts, "manifest.name") == [("tidepool", 2)]
    assert found(facts, "manifest.version") == [("0.4.2", 3)]
    assert found(facts, "manifest.description") == [("Tide tables in your terminal", 4)]
    assert found(facts, "manifest.keywords") == [(["tides", "cli"], 6)]
    assert found(facts, "manifest.platform") == [("cli", 2)]


def test_rust_library_has_no_platform(tmp_path):
    files = write(tmp_path, {"Cargo.toml": CARGO, "src/lib.rs": ""})
    assert found(list(CargoExtractor().extract(files)), "manifest.platform") == []


def test_rust_web_server_is_not_a_cli(tmp_path):
    files = write(tmp_path, {"Cargo.toml": CARGO + 'axum = "0.8"\n', "src/main.rs": ""})
    facts = list(CargoExtractor().extract(files))
    assert found(facts, "manifest.framework") == [("axum", 10)]
    assert found(facts, "manifest.platform") == [("web", 10)]


def test_cargo_workspace_root_is_skipped(tmp_path):
    files = write(tmp_path, {"Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n'})
    assert list(CargoExtractor().extract(files)) == []


# -------------------------------------------------------------- pyproject
def test_python_project(tmp_path):
    facts = list(PyprojectExtractor().extract(write(tmp_path, {"pyproject.toml": PYPROJECT})))
    assert found(facts, "manifest.name") == [("ledgerly", 2)]
    assert found(facts, "manifest.version") == [("1.3.0", 3)]
    assert found(facts, "manifest.homepage") == [("https://ledgerly.example", 12)]
    assert found(facts, "manifest.framework") == [("fastapi", None)]
    assert [value for value, _ in found(facts, "manifest.platform")] == ["web", "cli"]


def test_poetry_project(tmp_path):
    pyproject = '[tool.poetry]\nname = "shopkeep"\n\n[tool.poetry.dependencies]\ndjango = "^5"\n'
    facts = list(PyprojectExtractor().extract(write(tmp_path, {"pyproject.toml": pyproject})))
    assert found(facts, "manifest.name") == [("shopkeep", 2)]
    assert found(facts, "manifest.framework") == [("django", None)]


@pytest.mark.parametrize("content", ["[tool.ruff]\nline-length = 100\n", "[project\nbroken", ""])
def test_pyproject_without_a_project_is_skipped(tmp_path, content):
    assert list(PyprojectExtractor().extract(write(tmp_path, {"pyproject.toml": content}))) == []


# ----------------------------------------------------------------- go.mod
def test_go_command(tmp_path):
    files = write(
        tmp_path,
        {"go.mod": "module github.com/acme/harbor/v2\n\ngo 1.23\n", "cmd/harbor/main.go": ""},
    )
    facts = list(GoModExtractor().extract(files))
    assert found(facts, "manifest.name") == [("harbor", 1)]
    assert found(facts, "manifest.module") == [("github.com/acme/harbor/v2", 1)]
    assert [(f.value, f.file) for f in facts if f.kind == "manifest.platform"] == [
        ("cli", "cmd/harbor/main.go")
    ]


def test_go_library_and_garbage(tmp_path):
    library = list(
        GoModExtractor().extract(write(tmp_path, {"go.mod": "module example.com/lib\n"}))
    )
    assert found(library, "manifest.platform") == []
    assert list(GoModExtractor().extract(write(tmp_path, {"go.mod": "go 1.23\n"}))) == []
